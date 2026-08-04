#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# src/mdpoe.py
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
import struct
import string


ETH_HDR_SIZE         = 14
MDPoE_HDR_SIZE       = 46
MDPoE_MAX_DATA_SIZE  = (1500 - MDPoE_HDR_SIZE)

MDPoE_CMD_FIND_TARGET = 0x0000
MDPoE_CMD_DATA2TARGET = 0x0001
MDPoE_CMD_DATA2HOST   = 0x8000

MDPoE_PROTOCOL_OPEN    = 0x0000
MDPoE_PROTOCOL_SCP     = 0x0001



class EthHdr:
    """
    Ethernet layer packet, set dst_mac, src_mac and eth_type
    """
    my_mac_addr         = None
    target_mac_addr     = None
    broad_mac_addr      = bytearray(b'\xFF\xFF\xFF\xFF\xFF\xFF')
    mdpoe_eth_type      = b'\x4B\x1D'

    def __init__(self, data=None):
        self.dst            = bytearray(6)
        self.src            = bytearray(6)
        self.eth_type       = bytearray(2)
        self.data_bin       = None

        if EthHdr.my_mac_addr is None:
            # todo: get my mac addr
            EthHdr.my_mac_addr  = (b'\x08\x00\x27\xB9\x26\xDE')

        if data is not None:
            self.parseData(data)
        else:
            self.dst         = EthHdr.target_mac_addr
            self.src         = EthHdr.my_mac_addr
            self.eth_type    = EthHdr.mdpoe_eth_type

    def parseData(self, data):
        """
        Fill the Eth command header with `data`

        :Parameter data: data to parse
        """
        self.dst         = data[0:6]
        self.src         = data[6:12]
        self.eth_type    = data[12:14]
        self.data_bin    = data[14:]
        #r = struct.unpack_from('>6s6sH', data, 0)
        #(self.dst, self.src, self.protocol) = r
        

    def __eq__(self, other):
        """
        Implement == operator
        """
        if  (self.dst == other.src) and     \
            (self.src == other.dst) and     \
            (self.eth_type == other.eth_type):
            return True
        else:
            return False

    def __str__(self):
        """
        Implement `str()` method
        """
        ret = 'Ethernet Header:\n'
        ret += "Dst  : %02X:%02X:%02X:%02X:%02X:%02X" % struct.unpack_from(">6B",self.dst) + '\n'
        ret += "Src  : %02X:%02X:%02X:%02X:%02X:%02X" % struct.unpack_from(">6B",self.src) + '\n'
        ret += "Prtcl: %04X" % struct.unpack_from(">H",self.eth_type) + '\n'
        return ret


class MDPoEHdr:
    """
    MDPoE packet
    """

    # sequence number,
    session_seq_num = 0

    def __init__(self, data=None):
        self.cmd            = 0x0000
        self.seq_num        = self.session_seq_num
        self.split_num      = 0x0000
        self.err_code       = 0x0000
        self.protocol       = 0x0000
        self.datalen        = 0x0000
        self.rfu            = bytearray(34)
        self.scp_data       = None

        if data is not None:
            self.parseData(data)

    def parseData(self, data):
        """
        Fill the MDPoE command header with `data`

        :Parameter data: data to parse
        """
        r = struct.unpack_from('>6H', data, 0)
        (self.cmd, self.seq_num, self.split_num, self.err_code, self.protocol, self.datalen) = r
        self.rfu        = data[12:46]
        self.scp_data   = data[46:]

    def __eq__(self, other):
        if (self.seq_num    == other.seq_num)       and \
           (self.split_num  == other.split_num)     and \
           (self.err_code   == other.err_code)      and \
           (self.protocol   == other.protocol)      and \
           (self.datalen    <= MDPoE_MAX_DATA_SIZE) and \
           (self.rfu        == other.rfu)  :
            return True
        else:
            return False

    def increase_seq_num(self):
        # increase seq num
        MDPoEHdr.session_seq_num += 1
        if MDPoEHdr.session_seq_num > 65535:
            MDPoEHdr.session_seq_num = 0 

    def __str__(self):
        """
        Implement `str()` method
        """
        ret = 'MDPoE Header:\n'
        ret += "Cmd       : {}".format(self.cmd)            + '\n'
        ret += "SeqNum    : {}".format(self.seq_num)        + '\n'
        ret += "SplitNum  : {}".format(self.split_num)      + '\n'
        ret += "ErrCode   : {}".format(self.err_code)       + '\n'
        ret += "Protocol  : {}".format(self.protocol)       + '\n'
        ret += "Datalen   : {}".format(self.datalen)        + '\n'
        return ret


class FindTargetPacket():
    """
    Find target packet
    """

    def __init__(self):
        self.eth_hdr = EthHdr()
        self.mdpoe_hdr = MDPoEHdr()

        # configure
        self.eth_hdr.dst = EthHdr.broad_mac_addr
        self.mdpoe_hdr.cmd      = MDPoE_CMD_FIND_TARGET
        self.mdpoe_hdr.protocol = MDPoE_PROTOCOL_OPEN;

        # generate packet
        self.data_bin  = self.eth_hdr.dst + self.eth_hdr.src + self.eth_hdr.eth_type
        self.data_bin += struct.pack('>6H',  self.mdpoe_hdr.cmd, self.mdpoe_hdr.seq_num, self.mdpoe_hdr.split_num, \
                                        self.mdpoe_hdr.err_code, self.mdpoe_hdr.protocol, self.mdpoe_hdr.datalen) 
        self.data_bin += self.mdpoe_hdr.rfu

    def check(self, rcv_packet):
        eth_hdr = EthHdr(rcv_packet)
        
        if  (self.eth_hdr.src != eth_hdr.dst) or (self.eth_hdr.eth_type != eth_hdr.eth_type):
            print("Error! Eth Header does not match")
            print ("Send Packet:\n" + str(self.eth_hdr))
            print ("Rcv  Packet:\n" + str(eth_hdr))
            return False

        # set target mac addr
        EthHdr.target_mac_addr = eth_hdr.src

        mdpoehdr = MDPoEHdr(eth_hdr.data_bin)

        if mdpoehdr != self.mdpoe_hdr:
            print("Error! MDPoE Header does not match")
            print ("Send Packet:\n" + str(self.mdpoe_hdr))
            print ("Rcv  Packet:\n" + str(mdpoehdr))
            return False

        if mdpoehdr.cmd != MDPoE_CMD_FIND_TARGET:
            print("Error! cmd {}".format(mdpoehdr.cmd))
            return False

        # increase seq num
        self.mdpoe_hdr.increase_seq_num()

        return True

class Data2TargetPacket():
    """
    Data to target packet
    """

    def __init__(self, scp_packet):
        self.eth_hdr = EthHdr()
        self.mdpoe_hdr = MDPoEHdr()

        # configure
        self.mdpoe_hdr.cmd        = MDPoE_CMD_DATA2TARGET
        self.mdpoe_hdr.protocol   = MDPoE_PROTOCOL_SCP
        self.mdpoe_hdr.scp_packet = scp_packet
        self.mdpoe_hdr.datalen    = len(scp_packet)

        # generate packet
        self.data_bin  = self.eth_hdr.dst + self.eth_hdr.src + self.eth_hdr.eth_type
        self.data_bin += struct.pack('>6H',  self.mdpoe_hdr.cmd, self.mdpoe_hdr.seq_num, self.mdpoe_hdr.split_num,\
                                        self.mdpoe_hdr.err_code, self.mdpoe_hdr.protocol, self.mdpoe_hdr.datalen) 
        self.data_bin += self.mdpoe_hdr.rfu
        self.data_bin += self.mdpoe_hdr.scp_packet
        
    def check(self, rcv_packet):

        eth_hdr = EthHdr(rcv_packet)

        if eth_hdr != self.eth_hdr:
            print("Error! Eth Header does not match")
            print ("Send Packet:\n" + str(self.eth_hdr))
            print ("Rcv  Packet:\n" + str(eth_hdr))
            return False

        mdpoehdr = MDPoEHdr(eth_hdr.data_bin)

        if mdpoehdr != self.mdpoe_hdr:
            print("Error! MDPoE Header does not match")
            print ("Send Packet:\n" + str(self.mdpoe_hdr))
            print ("Rcv  Packet:\n" + str(mdpoehdr))
            return False

        if mdpoehdr.cmd != MDPoE_CMD_DATA2HOST:
            print("Error! cmd {}".format(mdpoehdr.cmd))
            return False

        # increase seq num
        self.mdpoe_hdr.increase_seq_num()

        return True

class Data2HostPacket(Data2TargetPacket):
    """
    Data to host packet
    """

    def __init__(self, scp_packet):
        Data2TargetPacket().__init__()

        # configure
        self.mdpoe_hdr.cmd    = MDPoE_CMD_DATA2HOST

        # generate packet
        #...
  

if __name__ == '__main__':
    print ("Stand alone package tests")
    # 
    print("FindTarget Package")
    findTarget = FindTargetPacket()
    print(list(findTarget.data_bin))
    rcv_packet = list(EthHdr.my_mac_addr) + [0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 75, 29, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0] 
    findTarget.check(bytearray(rcv_packet))

    #
    print ("\nDataToTarget")
    data2target = Data2TargetPacket(bytearray(b'\xbe\xef\xed\x06\x00\x00\x90\xc7'))
    print(list(data2target.data_bin))
    rcv_packet = list(EthHdr.my_mac_addr) + [0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 75, 29, 128, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0] 
    data2target.check(bytearray(rcv_packet)) 

    #
    # below script works only on linux machine, no windows
    #
    import os
    import time
    import socket
    import fcntl
    
    print ("\n\nTesting with bootloader")

    # Ethernet Socket Setup
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    fcntl.fcntl(sock, fcntl.F_SETFL, os.O_NONBLOCK)
    sock.bind(('enp0s3', socket.SOCK_RAW))

    # reset seq num
    MDPoEHdr.session_seq_num = 0

    for i in range(20):
        try:
            print ("\nTry {} ----------------------------------------------------------------------".format(i))
            findTarget = FindTargetPacket()

            send_len = sock.send(findTarget.data_bin)
            print("Host to Target --> Send len:{}".format(send_len))
            
            time.sleep(0.4)

            rcv_pkt = sock.recv(ETH_HDR_SIZE + MDPoE_HDR_SIZE)
            print ("\nSocket Read Len: {}".format(len(rcv_pkt)))
            result = findTarget.check(rcv_pkt)

            if result == True:
                print('Check OK\n')
            else:
                print('Check NOK\n')

        except:
            print('No Response RCV\n')
            pass
            