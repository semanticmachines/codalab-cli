#!/usr/bin/env python2.7
# For information about the design of the worker, see design.pdf in the same
# directory as this file. For information about running a worker, see the
# tutorial on the CodaLab Wiki.

import argparse
import getpass
import os
import logging
import signal
import socket
import stat
import sys
import multiprocessing
import re

from bundle_service_client import BundleServiceClient
from docker_client import DockerClient
from formatting import parse_size
from worker import Worker

logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser(description='CodaLab worker.')
    parser.add_argument('--tag',
                        help='Tag that allows for scheduling runs on specific '
                             'workers.')
    parser.add_argument('--server', default='https://worksheets.codalab.org',
                        help='URL of the CodaLab server, in the format '
                             '<http|https>://<hostname>[:<port>] (e.g., https://worksheets.codalab.org)')
    parser.add_argument('--work-dir', default='codalab-worker-scratch',
                        help='Directory where to store temporary bundle data, '
                             'including dependencies and the data from run '
                             'bundles.')
    parser.add_argument('--network-prefix', default='codalab_worker_network',
                        help='Docker network name prefix')
    parser.add_argument('--cpuset', type=str, metavar='CPUSET_STR', default='ALL',
                        help='Comma-separated list of CPUs in which to allow bundle execution, '
                             '(e.g., \"0,2,3\", \"1\").')
    parser.add_argument('--gpuset', type=str, metavar='GPUSET_STR', default='ALL',
                        help='Comma-separated list of GPUs in which to allow bundle execution '
                             '(e.g., \"0,1\", \"1\").')
    parser.add_argument('--max-work-dir-size', type=str, metavar='SIZE', default='10g',
                        help='Maximum size of the temporary bundle data '
                             '(e.g., 3, 3k, 3m, 3g, 3t).')
    parser.add_argument('--max-dependencies-serialized-length', type=int, default=60000,
                        help='Maximum length of serialized json of dependency list of worker '
                             '(e.g., 50, 30000, 60000).')
    parser.add_argument('--max-image-cache-size', type=str, metavar='SIZE',
                        help='Limit the disk space used to cache Docker images '
                             'for worker jobs to the specified amount (e.g. '
                             '3, 3k, 3m, 3g, 3t). If the limit is exceeded, '
                             'the least recently used images are removed first. '
                             'Worker will not remove any images if this option '
                             'is not specified.')
    parser.add_argument('--password-file',
                        help='Path to the file containing the username and '
                             'password for logging into the bundle service, '
                             'each on a separate line. If not specified, the '
                             'password is read from standard input.')
    parser.add_argument('--verbose', action='store_true',
                        help='Whether to output verbose log messages.')
    parser.add_argument('--id', default='%s(%d)' % (socket.gethostname(), os.getpid()),
                        help='Internal use: ID to use for the worker.')
    parser.add_argument('--shared-file-system', action='store_true',
                        help='Internal use: Whether the file system containing '
                             'bundle data is shared between the bundle service '
                             'and the worker.')
    parser.add_argument('--batch-queue',
                        help='Name of the AWS Batch queue to use for run submission. '
                             'Providing this option will cause runs to be submitted to Batch rather than local docker. '
                             'The queue must already exist and you must have AWS credentials to submit to it.'
                        )
    args = parser.parse_args()

    # Get the username and password.
    logger.info('Connecting to %s' % args.server)
    if args.password_file:
        if os.stat(args.password_file).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            print >>sys.stderr, """
Permissions on password file are too lax.
Only the user should be allowed to access the file.
On Linux, run:
chmod 600 %s""" % args.password_file
            exit(1)
        with open(args.password_file) as f:
            username = f.readline().strip()
            password = f.readline().strip()
    else:
        username = os.environ.get('CODALAB_USERNAME')
        if username is None:
            username = raw_input('Username: ')
        password = os.environ.get('CODALAB_PASSWORD')
        if password is None:
            password = getpass.getpass()

    # Set up logging.
    if args.verbose:
        logging.basicConfig(format='%(asctime)s %(message)s',
                            level=logging.DEBUG)
    else:
        logging.basicConfig(format='%(asctime)s %(message)s',
                            level=logging.INFO)

    max_work_dir_size_bytes = parse_size(args.max_work_dir_size)
    max_dependencies_serialized_length = args.max_dependencies_serialized_length
    if args.max_image_cache_size is None:
        max_images_bytes = None
    else:
        max_images_bytes = parse_size(args.max_image_cache_size)

    bundle_service = BundleServiceClient(args.server, username, password)

    # TODO Break the dependency of RunManagers on Worker to make this initialization nicer
    def create_run_manager(w):
        if args.batch_queue is None:
            # We defer importing the run managers so their dependencies are lazily loaded
            from docker_run import DockerRunManager
            from docker_client import DockerClient
            from docker_image_manager import DockerImageManager

            logging.info("Using local docker client for run submission.")

            docker = DockerClient()
            image_manager = DockerImageManager(docker, args.work_dir, max_images_bytes)
            cpuset = parse_cpuset_args(args.cpuset)
            gpuset = parse_gpuset_args(docker, args.gpuset)
            return DockerRunManager(docker, bundle_service, image_manager, w, args.network_prefix, cpuset, gpuset)
        else:
            try:
                import boto3
            except ImportError:
                logging.exception("Missing dependencies, please install boto3 to enable AWS support.")
                import sys
                sys.exit(1)

            from aws_batch import AwsBatchRunManager

            logging.info("Using AWS Batch queue %s for run submission.", args.batch_queue)

            batch_client = boto3.client('batch')
            return AwsBatchRunManager(batch_client, args.batch_queue, bundle_service, w)

    worker = Worker(args.id, args.tag, args.work_dir, max_work_dir_size_bytes, max_dependencies_serialized_length,
                    args.shared_file_system, bundle_service, create_run_manager)

    # Register a signal handler to ensure safe shutdown.
    for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]:
        signal.signal(sig, lambda signup, frame: worker.signal())

    # BEGIN: DO NOT CHANGE THIS LINE UNLESS YOU KNOW WHAT YOU ARE DOING
    # THIS IS HERE TO KEEP TEST-CLI FROM HANGING
    print('Worker started.')
    # END

    worker.run()


def parse_cpuset_args(arg):
    """
    Parse given arg into a set of integers representing cpus

    Arguments:
        arg: comma seperated string of ints, or "ALL" representing all available cpus
    """
    cpu_count = multiprocessing.cpu_count()
    if arg == 'ALL':
        cpuset = range(cpu_count)
    else:
        try:
            cpuset = [int(s) for s in arg.split(',')]
        except Exception, e:
            raise ValueError("CPUSET_STR invalid format: must be a string of comma-separated integers")

        if not len(cpuset) == len(set(cpuset)):
            raise ValueError("CPUSET_STR invalid: CPUs not distinct values")
        if not all(cpu in range(cpu_count) for cpu in cpuset):
            raise ValueError("CPUSET_STR invalid: CPUs out of range")
    return set(cpuset)


def parse_gpuset_args(docker_client, arg):
    """
    Parse given arg into a set of integers representing gpu devices

    Arguments:
        docker_client: DockerClient instance
        arg: comma seperated string of ints, or "ALL" representing all gpus
    """
    if arg == '':
        return set()

    info = docker_client.get_nvidia_devices_info()
    all_gpus = []
    if info is not None:
        for d in info['Devices']:
            m = re.search('^/dev/nvidia(\d+)$', d['Path'])
            all_gpus.append(int(m.group(1)))

    if arg == 'ALL':
        return set(all_gpus)
    else:
        try:
            gpuset = [int(s) for s in arg.split(',')]
        except Exception, e:
            raise ValueError("GPUSET_STR invalid format: must be a string of comma-separated integers")

        if not len(gpuset) == len(set(gpuset)):
            raise ValueError("GPUSET_STR invalid: GPUs not distinct values")
        if not all(gpu in all_gpus for gpu in gpuset):
            raise ValueError("GPUSET_STR invalid: GPUs out of range")
        return set(gpuset)


if __name__ == '__main__':
    main()
