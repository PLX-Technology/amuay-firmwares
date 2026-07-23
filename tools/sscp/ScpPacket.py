#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# src/ScpPacket.py
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

from BootloaderScp import BootloaderScp, ScpCmd
from utils import *

__author__ = "MAXIM Integrated <https://www.maximintegrated.com/>"

_ERROR_MSG = [['  ' for i in range(40)] for j in range(20)]


def load_error_file(chip_code, rom_code, verbose):
    """
    Load the error file according to the chip code as define in SPEC98H06RevA-ARM_secure_micros_USN_format
    and the rom code version. The error message array is store in the global _ERROR_MSG variable
    :param chip_code:
    :param rom_code:
    :param verbose:
    :return:
    """
    global _ERROR_MSG

    # convert to upper case
    chip_code = chip_code.upper()

    if chip_code == "00":
        rom_code = "00000000"
        if verbose >= VERBOSE:
            print ("\n\tSample chip, Error identification may not work correctly")

    rom_specific_errors = "errors.max" + chip_code + "_" + rom_code + "_error"
    try:
        _CHIP_ERROR = __import__(rom_specific_errors, globals(), locals(), ['ERROR_MSG'], 0)
        
        _ERROR_MSG = _CHIP_ERROR.ERROR_MSG
    except ImportError:
        #if verbose >= VERBOSE:
        #    print ("\tUnknown chip " + rom_specific_errors + " file is not added yet")
        pass


class ScpPacket:

    is_connection_packet = False

    def __init__(self, packet_name, packet_data, options, cmd, id, way):
        self.bl_scp = None
        self.scp_cmd = None
        self.timeout = options.timeout
        self.way = way
        self.cmd = cmd
        self.id = id
        self.file_name = packet_name
        self.verbose = options.verbose
        self.chip_name = options.chip_name
        self.mpc = options.mpc

        if self.way:
            self.packet_data = packet_data
        else:
            self.packet_data = ScpCmd(packet_data)

    def process(self, bl_scp):
        self.bl_scp = bl_scp
        if self.way:
            self.send()
        else:
            self.receive()

    def send(self):
        if self.verbose >= EXTRA_VERBOSE:
            print(self.id + ' SEND> ' + self.cmd)

        try:
            self.bl_scp.writePacket(self.packet_data, self.timeout)
        except KeyboardInterrupt:
            raise KeyboardInterrupt()
        except Exception as inst:
            if not self.mpc:
                print(inst)
                print_err("Error" + self.file_name)
            raise Exception()

    def receive(self):
        if self.verbose >= EXTRA_VERBOSE:
                print(self.id + ' WAIT> ' + self.cmd)
        try:
            self.scp_cmd = self.bl_scp.readPacket(self.is_connection_packet)
            if self.scp_cmd is None:
                if not self.mpc:
                    print_err("\nerror: receiving packet failed")
                raise Exception()

            self.check()

        except KeyboardInterrupt:
            raise KeyboardInterrupt()
        except:
            if not self.is_connection_packet:
                if not self.mpc:
                    print_err("\nerror: read packet timeout error occur. #")
            raise

    def check(self):
        if self.packet_data != self.scp_cmd:                      
            err_code = self.scp_cmd.data_bin[4]
            module   = self.scp_cmd.data_bin[7]
            if not self.mpc:
                msg = f"\nerror: received packet is not the expected one, module={module}, code={err_code}"
                print_err(msg)
                if ( module < len(_ERROR_MSG) ) and ( err_code < len(_ERROR_MSG[module]) ):
                    print_err("error: " + _ERROR_MSG[module][err_code] + " (" + hex(module)[2:] + "," + hex(err_code)[2:] + ")")
                if self.verbose >= EXTRA_VERBOSE:
                    print("======================================")
                    print("Expected Command")
                    print(self.packet_data)
                    print("--------------------------------------")
                    print("Received Command")
                    print(self.scp_cmd)
                    print("======================================")
            else:
                mpc_error(module, err_code)
            raise Exception()


class HelloReplyPacket(ScpPacket):
    def __init__(self, packet_name, packet_data, options, cmd, id, way):
        ScpPacket.__init__(self, packet_name, packet_data, options, cmd, id, way)

    def check(self):
        self.parse_hello_reply()

    def parse_hello_reply(self):
        """ Parse and print useful information from hello_reply packet
            Also load Error message
        :return:
        """
        usn = self.scp_cmd.data[44:70].decode('utf-8')
        rom_ver = self.scp_cmd.data[28:36].decode('utf-8')
        phase = self.scp_cmd.data[36:38].decode('utf-8')
        config = self.scp_cmd.data[42:44].decode('utf-8')
        # convert hex to int
        config = int(config, 16)
        """
        Config byte bit fields
            Bit 7: Indicates JTAG staus
            Bit 6: Indicates Rework status
            Bit 5: Indicates TM mode        (Not support for some devices)
            Bit 4: Indicates CRK status     (Not support for some devices)
            Bit 3-0: Not used
        """

        if not self.mpc:
            load_error_file(usn[2:4], rom_ver, self.verbose)

        if self.verbose >= VERBOSE:
            print("\n======================================")
            print("\tROM Version : {}{}{}{}".format(rom_ver[6:8],rom_ver[4:6],rom_ver[2:4],rom_ver[0:2]))
            print("\tPhase : " + str(phase))
            print("\tRework : " + ("Available" if (config & 0x40) else "Not Available"))
            print("\tJTAG : " + ("Locked" if (config & 0x80) else "Not Locked"))
            #
            # Below devices does not send CRK status in config byte
            #
            if self.chip_name not in ("MAX32510", "MAX32555", "MAX32558", 
                                      "MAX32552", "MAX32560", "MAX32561",
                                      "MAX32590", "MAX32591", "MAX32592", 
                                      "MAX32550", 
                                      "MAX32565",
                                      "MAX32651", 
                                      "MAX32666"):
                print("\tCRK : " + ("Not Exist" if (config & 0x10) else "Exist"))            
            print("\tUSN : " + str(usn))
            print("======================================")
        if self.mpc:
                mpc_chip_info(usn, phase)


class ErasePacket(ScpPacket):
    def __init__(self, packet_name, packet_data, options, cmd, id, way):
        ScpPacket.__init__(self, packet_name, packet_data, options, cmd, id, way)
        self.timeout = options.erase_timeout


class ConnectionPacket(ScpPacket):
    def __init__(self, packet_name, packet_data, options, cmd, id, way):
        ScpPacket.__init__(self, packet_name, packet_data, options, cmd, id, way)
        self.timeout = 0.2
        self.is_connection_packet = True

    def check(self):
        if self.packet_data != self.scp_cmd:
            raise Exception()


class WriteCRKPacket(ScpPacket):

    def __init__(self, packet_name, packet_data, options, cmd, id, way):
        ScpPacket.__init__(self, packet_name, packet_data, options, cmd, id, way)

    def check(self):
        if self.packet_data != self.scp_cmd:
            err_code = self.scp_cmd.data_bin[4]
            module = self.scp_cmd.data_bin[7]
            if module == 0x87 and err_code == 0x08:
                print_err("error: " + hex(err_code) + " module : " + hex(module))
                raise FlashCRKWriteError()
        ScpPacket.check(self)


class DumpPacket(ScpPacket):
    dump_filename = None

    def __init__(self, packet_name, packet_data, options, cmd, id, way):
        self.bl_scp = None
        self.scp_cmd = None
        self.timeout = options.timeout
        self.way = way
        self.cmd = cmd
        self.id = id
        self.file_name = packet_name
        self.dump_filename = options.dump_filename

    def check(self):
        if self.packet_data != self.scp_cmd:
            raise Exception()

    def receive(self):
        print("")
        self.bl_scp.readDump(self.dump_filename)
