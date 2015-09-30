FROM ubuntu:14.04
MAINTAINER Cassio Paes-Leme <cassio@delectable.com>

# A docker container with the Nvidia kernel module and CUDA drivers installed

ENV CUDA_RUN http://developer.download.nvidia.com/compute/cuda/7_0/Prod/local_installers/cuda_7.0.28_linux.run

RUN apt-get update && apt-get install -q -y \
  wget \
  build-essential 

RUN cd /opt && \
  wget $CUDA_RUN && \
  chmod +x *.run && \
  mkdir nvidia_installers && \
  ./cuda_7.0.28_linux.run -extract=`pwd`/nvidia_installers 

RUN apt-get -y install linux-source

RUN apt-get -y install linux-headers-`uname -r`

RUN cd /opt/nvidia_installers && \
    ./NVIDIA-Linux-x86_64-346.46.run -s -N --no-kernel-module

RUN cd /opt/nvidia_installers && \
  ./cuda-linux64-rel-7.0.28-19326674.run -noprompt

# Ensure the CUDA libs and binaries are in the correct environment variables
ENV LD_LIBRARY_PATH=:/usr/local/cuda-7.0/lib64

ENV PATH=$PATH:/usr/local/cuda-7.0/bin

RUN cd /opt/nvidia_installers && \
  ./cuda-samples-linux-7.0.28-19326674.run -noprompt

#get CUDNN from s3
RUN cd /opt && \
    wget https://s3.amazonaws.com/delectable-assets/public/cudnn-6.5-linux-x64-v2.tgz && \
    tar xzf cudnn-6.5-linux-x64-v2.tgz && cd cudnn-6.5-linux-x64-v2 && \
    cp lib* /usr/local/cuda/lib64/ && \
    cp cudnn.h /usr/local/cuda/include/

RUN apt-get install -y libprotobuf-dev libleveldb-dev libsnappy-dev libopencv-dev libboost-all-dev \
    libhdf5-serial-dev protobuf-compiler gfortran libjpeg62 libfreeimage-dev libatlas-base-dev git \
    python-dev python-pip libgoogle-glog-dev libbz2-dev libxml2-dev libxslt-dev libffi-dev libssl-dev \
    libgflags-dev liblmdb-dev python-yaml python-numpy

RUN easy_install pillow

# Add caffe binaries to path
ENV PATH $PATH:/opt/caffe/.build_release/tools

RUN apt-get install -y software-properties-common

RUN add-apt-repository -y ppa:mc3man/trusty-media && \
    apt-get update && \
    apt-get install -y ffmpeg gstreamer0.10-ffmpeg

# Get dependencies
RUN apt-get update && apt-get install -y \
    bc cmake curl gcc-4.6 g++-4.6 gcc-4.6-multilib g++-4.6-multilib gfortran \
    git libprotobuf-dev libleveldb-dev libsnappy-dev libopencv-dev libboost-all-dev \ 
    libhdf5-serial-dev liblmdb-dev libjpeg62 libfreeimage-dev libatlas-base-dev pkgconf \
    protobuf-compiler python-dev python-pip python-numpy python-scipy python-dev python-nose \
    unzip wget python-protobuf protobuf-compiler libgoogle-glog-dev libgoogle-glog0 python-google-apputils \
    python-gflags hdf5-helpers libhdf5-dev libhdf5-serial-dev libhdf5-7 python-tables-data python-tables \
    python-tables-lib libleveldb-dev libleveldb1 libsnappy-dev libsnappy1 liblmdb-dev liblmdb0 \
    libatlas3-base libatlas-dev libatlas-base-dev python-pandas-lib python-pandas python-skimage libyaml-dev \
    checkinstall yasm libjpeg-dev libjasper-dev libavcodec-dev libavformat-dev libswscale-dev \
    libdc1394-22-dev libxine-dev libgstreamer0.10-dev libgstreamer-plugins-base0.10-dev \
    libv4l-dev python-dev python-numpy libtbb-dev libqt4-dev libgtk2.0-dev libfaac-dev libmp3lame-dev \
    libopencore-amrnb-dev libopencore-amrwb-dev libtheora-dev libvorbis-dev libxvidcore-dev x264 v4l-utils qt5-default

RUN pip install -U scikit-image

RUN C_INCLUDE_PATH=/usr/lib/openmpi/include pip install --upgrade tables

RUN apt-get -y install cython

RUN cd /opt && git clone https://github.com/NVIDIA/DIGITS.git digits && cd digits && \ 
    git checkout 85c51f0f2d && pip install -r requirements.txt && \
  git clone --branch caffe-0.12 https://github.com/NVIDIA/caffe.git && \
  cd caffe && cp Makefile.config.example Makefile.config && make -j32 && make pycaffe && \
    apt-get install -y graphviz

ADD digits.cfg /opt/digits/digits/digits.cfg

WORKDIR /opt/digits

EXPOSE 8080