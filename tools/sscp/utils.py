#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# src/utils.py
#
# ******************************************************************************
# Copyright (C) Maxim Integrated Products, Inc., All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL MAXIM INTEGRATED BE LIABLE FOR ANY CLAIM, DAMAGES
# OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# Except as contained in this notice, the name of Maxim Integrated
# Products, Inc. shall not be used except as stated in the Maxim Integrated
# Products, Inc. Branding Policy.
#
# The mere transfer of this software does not imply any licenses
# of trade secrets, proprietary technology, copyrights, patents,
# trademarks, maskwork rights, or any other form of intellectual
# property whatsoever. Maxim Integrated Products, Inc. retains all
# ownership rights.
#******************************************************************************
#
#

import sys
import os

__author__ = "MAXIM Integrated <https://www.maximintegrated.com/>"

VERBOSE = 1
EXTRA_VERBOSE = 2
DEBUG = 3

INTERFACE_UART='uart'
INTERFACE_USB='usb'
INTERFACE_ETH='eth'
INTERFACE_SPI='spi'

class Error(Exception):
    """Base class for exceptions in this module."""
    pass


class FlashCRKWriteError(Error):
    """Base class for exceptions in this module."""
    pass


class FileError(Error):
    """Base class for exceptions in this module."""
    pass


class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def print_err(text):
    print(bcolors.FAIL + text + bcolors.ENDC, file=sys.stderr)


def print_ok(text):
    print(bcolors.OKGREEN + text + bcolors.ENDC)


def get_fullpath(file_dir, file_name):
    if file_dir == "":
        return file_name
    if os.name == "posix":
        return file_dir + '/' + file_name
    if os.name == "nt":
        return file_dir + '\\' + file_name


def mpc_chip_info(usn, phase):
    print('{:0>2X}{:0>2X}{:0>26X}{:0>2X}'.format(2, 9, int(usn, 16), int(phase)))


def mpc_status(current, total):
    print('{:0>2X}{:0>2X}{:0>4X}{:0>4X}'.format(1, 4, current, total))


def mpc_error(module, err):
    print('{:0>2X}{:0>2X}{:0>4X}{:0>4X}'.format(1, 4, module, err))
