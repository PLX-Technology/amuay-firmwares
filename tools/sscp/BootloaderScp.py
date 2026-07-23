#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# src/BootloaderScp.py
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

""" Bootloader SCP Classes

:Summary: Bootloader SCP Classes
:Author: MAXIM Integrated
:Organization: Maxim Integrated Products
:Copyright: Copyright © Maxim Integrated Products
:License: BSD License - http://www.opensource.org/licenses/bsd-license.php
"""

from struct import unpack
from binascii import hexlify
from utils import *
import serial
import time

__author__ = "MAXIM Integrated <https://www.maximintegrated.com/>"


class ScpCmdHdr:
    """
    SCP command header
    """

    FMT = '>cccBHBB'
    SIZE = 8

    """SYNC: """
    sync = None
    ctl = None
    dl = None
    id = None
    cks = None
    data_bin = None
    extra = None

    def __init__(self, data=None):
        """
        :Parameter data: data for initialization
        """
        if data is not None:
            self.extra = self.parseData(data)
            if self.extra is not None:
                print("waring: extra data in header", file=sys.stderr)

    def parseData(self, data):
        """
        Fill the SCP command header with `data`

        :Parameter data: data to parse
        """
        self.data_bin = data

        r = unpack(self.FMT, data)
        extra = None
        if len(r) == 9:
            (sync0, sync1, sync2, self.ctl, self.dl, self.id, self.cks, extra) = r
        else:
            (sync0, sync1, sync2, self.ctl, self.dl, self.id, self.cks) = r
        self.sync = (sync0, sync1, sync2)

        return extra

    def __eq__(self, other):
        """
        Implement == operator
        """
        return ((self.data_bin == other.data_bin))

    def __ne__(self, other):
        """
        Implement != operator
        """
        return ((self.data_bin != other.data_bin))

    def __str__(self):
        """
        Implement `str()` method
        """
        ret = 'ScpCmdHdr:\n'
        ret += '  - SYNC: ' + str(self.sync) + '\n'
        ret += '  - CTL: ' + str(self.ctl) + '\n'
        ret += '  - DL: ' + str(self.dl) + '\n'
        ret += '  - ID: ' + str(self.id) + '\n'
        ret += '  - CKS: ' + str(self.cks)
        return ret


class ScpCmd:
    """
    SCP command class
    """

    DATA_FMT = 's'
    CHK_FMT = 'I'

    hdr = None
    data = None
    chk = None
    len = 0
    data_bin = None
    data_len = 0

    def __init__(self, buffer=None):
        """
        :Parameter buffer: a data buffer implementing the method `read(size)`
        """

        if buffer is not None:
            self.parseData(buffer)

    def setHdr(self, hdr):
        """
        Set the SCP command header
        """

        self.hdr = hdr

    def parseData(self, buffer):
        """
        Fill the SCP command with data from `buffer`

        :Parameter buffer: buffer implementing the method `read(size)`
        """
        start = 0  #start
        end   = 8  #end

        # Read the header
        data = buffer[start:end]
        if len(data) != 8:
            print("error: expected hdr size != real one", file=sys.stderr)
            print("       real size = " + str(len(data)), file=sys.stderr)
            print("       expected size = 8", file=sys.stderr)
            raise Exception()

        self.hdr = ScpCmdHdr()
        self.len = 8
        self.hdr.parseData(data)

        if self.hdr.dl != 0:
            start = end
            end   = start + self.hdr.dl

            data = buffer[start:end]
            if len(data) != (self.hdr.dl):
                print("error: expected data size != real one", file=sys.stderr)
                print("       real size = " + str(len(data)), file=sys.stderr)
                print("       expected size = " + str((self.hdr.dl)), file=sys.stderr)
                raise Exception()

            self.data_bin = data
            self.data_len = self.hdr.dl

            #self.data = unpack(str(self.hdr.dl) + self.DATA_FMT, data)
            self.data = hexlify(data)

            start = end
            end   = start + 4
            data  = buffer[start:end]
            if len(data) != (4):
                print("error: expected chk size != real one", file=sys.stderr)
                print("       real size = " + str(len(data)), file=sys.stderr)
                print("       expected size = " + str(4), file=sys.stderr)
                raise Exception()

            #r = unpack(self.CHK_FMT, data)
            #(self.chk,) = r
            self.chk = hexlify(data)

        else:
            self.data = None
            self.chk = None


    def __eq__(self, other):
        """
        Implement == operator
        """
        return ((self.data_bin == other.data_bin) and
                (self.hdr.data_bin == other.hdr.data_bin))

    def __ne__(self, other):
        """
        Implement != operator
        """
        return ((self.data_bin != other.data_bin) or
                (self.hdr.data_bin != other.hdr.data_bin))

    def __str__(self):
        """
        Implement `str()` method
        """

        ret = 'ScpCmd:\n'
        ret += str(self.hdr) + '\n'
        ret += 'Data:\n' + str(self.data) + '\n'
        ret += 'Checksum: ' + str(self.chk)
        return ret


class BootloaderScp(serial.Serial):
    """
    BootloaderScp extends `Serial` by adding functions to read SCP commands.
    """

    def __init__(self, port, options):
        """
        :Parameter port: serial port to use (/dev/tty* or COM*)
        """

        if options.serial_parity == 'none':
            parity = serial.PARITY_NONE
        elif options.serial_parity == 'odd':
            parity = serial.PARITY_ODD
        elif options.serial_parity == 'even':
            parity = serial.PARITY_EVEN
        elif options.serial_parity == 'mark':
            parity = serial.PARITY_MARK
        elif options.serial_parity == 'space':
            parity = serial.PARITY_SPACE
        elif options.serial_parity == 'names':
            parity = serial.PARITY_NAMES
        else:
            print(("Unknown parity mode " + options.serial_parity + " Using NONE"))
            parity = serial.PARITY_NONE

        if options.serial_stopbits == '1':
            stopbits = serial.STOPBITS_ONE
        elif options.serial_stopbits == '1.5':
            stopbits = serial.STOPBITS_ONE_POINT_FIVE
        elif options.serial_stopbits == '2':
            stopbits = serial.STOPBITS_TWO
        else:
            print(("Unknown number of STOP bit  " + options.serial_stopbits + " Using 1 STOP bit"))
            stopbits = serial.STOPBITS_ONE

        if options.serial_bytesize == 5:
            bytesize = serial.FIVEBITS
        elif options.serial_bytesize == 6:
            bytesize = serial.SIXBITS
        elif options.serial_bytesize == 7:
            bytesize = serial.SEVENBITS
        elif options.serial_bytesize == 8:
            bytesize = serial.EIGHTBITS
        else:
            print(("Unknown byte size " + options.serial_bytesize + " Using 8 bits per byte"))
            bytesize = serial.EIGHTBITS

        serial.Serial.__init__(self, port=port,
                               baudrate=options.serial_baudrate,
                               timeout=options.timeout,
                               bytesize=bytesize,
                               stopbits=stopbits,
                               rtscts=options.serial_rtscts,
                               dsrdtr=options.serial_dsrdtr,
                               parity=parity,
                               xonxoff=options.serial_xonxoff)

        if sys.platform == 'linux':
            import fcntl
            fcntl.flock(self.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB )
 
        self.dtr = options.serial_dsrdtr
        self.rts = options.serial_rtscts
        
    def reset(self):
        self.setRTS(True)
        time.sleep(0.5)
        self.setRTS(False)

    def writePacket(self, packet_data, timeout=1):
        if self.timeout != timeout:
            self.timeout = timeout
        #print("Send packet Len:{}".format(len(packet_data)))
        #print(hexlify(packet_data))

        self.write(packet_data)
        self.flush()

    def readPacket(self, quiet):
        """
        Read a full packet
        """

        scp_cmd = ScpCmd()

        hdr = self.readHeader(quiet)
        if hdr is None:
            print("error: no header", file=sys.stderr)
            raise Exception()

        scp_cmd.setHdr(hdr)
        scp_cmd.len = 8

        if hdr.dl != 0:
            data = self.read(hdr.dl)

            if len(data) != hdr.dl:
                print_err("Error: expected data size != real one")
                print_err("\t\t real size = " + str(len(data)))
                print_err("\t\t expected size = " + str(hdr.dl))
                raise Exception()

            scp_cmd.len += hdr.dl
            scp_cmd.data = hexlify(data)
            scp_cmd.data_bin = data

            data = self.read(4)
            if len(data) != 4 and scp_cmd.hdr.ctl != 12:
                print_err("Error: expected chk size != real one")
                print_err("\t\t real size = " + str(len(data)))
                print_err("\t\t expected size = 4")
                raise Exception()

            scp_cmd.len += 4
            scp_cmd.chk = hexlify(data)

        return scp_cmd

    def readHeader(self, quiet):
        """
        Read the packet header
        """

        scp_hdr = ScpCmdHdr()

        data = self.read(scp_hdr.SIZE)
        if len(data) == 0:
            if not quiet:
                print_err("Error: timeout, no packet received")
            raise Exception()

        if len(data) != scp_hdr.SIZE:
            if not quiet:
                print_err("Error: expected hdr size != real one")
                print_err("\t\t real size = " + str(len(data)))
                print_err("\t\t expected size = " + str(scp_hdr.SIZE))
            raise Exception()

        scp_hdr.parseData(data)

        return scp_hdr

    def readDump(self, filename):
        print("\nFile Name:{}".format(filename))
        print ("Please wait, reading data...")
        all_data = ""
        while True:
            data = self.read(1).decode('utf-8')
            all_data += data
            if data == chr(4):
                if filename is not None:
                    f = open(filename, 'a')
                    f.write(all_data)
                    f.close()
                else:
                    sys.stdout.write(all_data)
                break

    def close(self):
        super(BootloaderScp, self).close()
