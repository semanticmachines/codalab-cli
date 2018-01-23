FROM ubuntu:14.04

MAINTAINER Percy Liang <pliang@cs.stanford.edu>

# Basic Python environment
RUN apt-get update
RUN apt-get install -y python-pip
RUN apt-get install -y python-dev

# Useful utilities
RUN apt-get install -y wget
RUN apt-get install -y screen
RUN pip install virtualenv

# Needed for CI
RUN apt-get install -y sshpass
RUN apt-get install -y mysql-client libmysqlclient-dev
