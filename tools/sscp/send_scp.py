#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# src/send_scp.py
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

""" Usage: send_scp [options] FILENAME

Options:
   | --version				  show program's version number and exit
   | -h, --help				 show this help message and exit
   | -s SERIAL, --serial=SERIAL define the serial port to use
   | -v, --verbose			  enable verbose mode
   | -l, --license			  display license
   | --list-serial			  display available serial ports
   | -b, --bl-emulation	      emulate the bootloader

serial_send sends signed packets to the bootloader on the serial link. This
tool can be used as a test tool for bootloader and validation tool. FILENAME
contains both sent and received packets. Those last ones are used for
verification.

:Summary: Bootloader SCP commands sender
:Version: #__VERSION__#

:Author: MAXIM Integrated
:Organization: Maxim Integrated Products
:Copyright: Copyright © Maxim Integrated Products
:License: BSD License - http://www.opensource.org/licenses/bsd-license.php
"""

# ---- IMPORTS


import re
import time
from progressbar import *
from serial import *

from optparse import OptionParser, OptionGroup
from serial.tools import list_ports
from scan import scan
from ScpPacket import *
from utils import *
import colorama
import zipfile

# ⚠️ VA DESPUES de los `import *` de arriba, y no es cosmetica.
# `from progressbar import *` importa la FUNCION progressbar() que exponen las
# versiones modernas de progressbar2, y esa funcion TAPA al modulo del mismo
# nombre. Las llamadas `progressbar.ProgressBar(...)` de mas abajo mueren con
#     'function' object has no attribute 'ProgressBar'  ->  SCP session FAILED
# ya con la sesion abierta, asi que PARECE un fallo de grabado y no lo es.
# Reimportar el modulo aqui devuelve el nombre a su sitio.
import progressbar

import sys
# ---- CONSTANTS

AUTHOR = "MAXIM Integrated"

VERSION = "#__VERSION__#"
PROG = "send_scp"

COPYRIGHT = "Copyright © Maxim Integrated Products"

LICENSE = """Copyright © Maxim Integrated Products, Inc., All Rights Reserved.

 Permission is hereby granted, free of charge, to any person obtaining a
 copy of this software and associated documentation files (the "Software"),
 to deal in the Software without restriction, including without limitation
 the rights to use, copy, modify, merge, publish, distribute, sublicense,
 and/or sell copies of the Software, and to permit persons to whom the
 Software is furnished to do so, subject to the following conditions:

 The above copyright notice and this permission notice shall be included
 in all copies or substantial portions of the Software.

 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
 OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
 IN NO EVENT SHALL MAXIM INTEGRATED BE LIABLE FOR ANY CLAIM, DAMAGES
 OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
 ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
 OTHER DEALINGS IN THE SOFTWARE.

 Except as contained in this notice, the name of Maxim Integrated
 Products, Inc. shall not be used except as stated in the Maxim Integrated
 Products, Inc. Branding Policy.

 The mere transfer of this software does not imply any licenses
 of trade secrets, proprietary technology, copyrights, patents,
 trademarks, maskwork rights, or any other form of intellectual
 property whatsoever. Maxim Integrated Products, Inc. retains all
 ownership rights."""

EPILOG = """serial_send sends signed packets to the bootloader on the serial
link. This tool can be used as a test tool for bootloader and validation tool.
FILENAME contains both sent and received packets. Those last ones are used for
verification"""

# ---- GLOBALS

_ERROR_MSG = [['  ' for i in range(40)] for j in range(20)]


def get_extra_packet():

	if 'MAXIM_SBT_DIR' not in os.environ:
		print_err("Environment Variable 'MAXIM_SBT_DIR' not correctly set.")
		raise Error
	if options.verbose >= DEBUG:
		print("Env MAXIM_SBT_DIR    :" + os.environ['MAXIM_SBT_DIR'])
		print("Device name          :" + options.chip_name)
		print("Param 'Extra_Packet' :" + options.extra_packet)

	# folder exist?
	packets = os.path.join(os.environ['MAXIM_SBT_DIR'], "devices", options.chip_name, "scp_packets", options.extra_packet, "packet.list")
	if os.path.isfile(packets):
		return packets

	# zip file exist?
	packets = os.path.join(os.environ['MAXIM_SBT_DIR'], "devices", options.chip_name, "scp_packets", options.extra_packet)
	if not packets.endswith(".zip"):
		packets = packets + ".zip"
	
	if os.path.isfile(packets):
		return packets

	print_err("Requested Extra Script packets does not exist " + options.extra_packet)
	raise FileError()

def list_extra_packets():

	if 'MAXIM_SBT_DIR' not in os.environ:
		print_err("Environment Variable 'MAXIM_SBT_DIR' not correctly set.")
		raise Error
	if options.verbose >= DEBUG:
		print("Env MAXIM_SBT_DIR :" + os.environ['MAXIM_SBT_DIR'])

	packets_dir = os.path.join(os.environ['MAXIM_SBT_DIR'], "devices", options.chip_name, "scp_packets")

	if os.path.isdir(packets_dir):
		print("\nAvailable packets for {}:".format(options.chip_name))
		i = 1
		for f in os.listdir(packets_dir):
			print("	{}- {}".format(i, f))
			i = i+1
	else:
		print ("Packets not found for {}".format(options.chip_name))
		return


def parse_scpcmd_file(filename, options):
	"""
	:param filename:
	:param options:
	:return:
	"""

	if options.verbose >= VERBOSE:
		print('Open file: ' + filename)

	scp_zip = None

	if filename.endswith(".zip"):
		scp_zip = zipfile.ZipFile(filename, 'r')
		packet_list_file = scp_zip.read("packet.list").decode()
	else:
		f = open(filename, "r")
		packet_list_file = f.read()
		f.close()
		file_dir = os.path.dirname(filename)
		
	packets = []

	# Get number of packets to send
	for line in packet_list_file.split("\n"):
		file_name = line.strip()
		if file_name == '':
			continue 

		s_m = re.search('(\w+[_-]*\w+)\.(\d+)\.(\w+[_-]*\w+)\.((\w+[_-]*)*)\.\w+', file_name)
		if s_m is not None:
			id = s_m.group(2)
			cmd = s_m.group(4)
			way_str = s_m.group(3)
		else:
			print_err("error: wrong filename: " + file_name)
			raise FileError()

		if way_str == 'bl':
			way = False ^ options.bl_emulation
		elif way_str == 'host':
			way = True ^ options.bl_emulation
		else:
			print_err("error: wrong filename: " + file_name)
			raise FileError()

		# read packet data
		if scp_zip:
			packet_data = scp_zip.read(file_name)
		else:
			with open(get_fullpath(file_dir, file_name), 'rb') as f:
				packet_data = f.read()

		if packet_data is None:
			print_err("Error : Unable to read file packet : " + file_dir)
			raise Exception()

		if cmd in ("connection_request", "connection_reply"):
			packet = ConnectionPacket(file_name, packet_data, options, cmd, id, way)
		elif cmd in ("hello_reply", ):
			packet = HelloReplyPacket(file_name, packet_data, options, cmd, id, way)
		elif cmd in ("erase_mem", "del_mem"):
			packet = ErasePacket(file_name, packet_data, options, cmd, id, way)
		elif cmd in ("dump", ):
			packet = DumpPacket(file_name, packet_data, options, cmd, id, way)
		elif cmd in ("write_crk_response", ):
			packet = WriteCRKPacket(file_name, packet_data, options, cmd, id, way)
		else:
			packet = ScpPacket(file_name, packet_data, options, cmd, id, way)

		packets.append(packet)

	# This function can return without closing .zip file by raising exception, not a problem, pass it for now
	if scp_zip:
		scp_zip.close()

	return packets


def process_packet(packet_list, options, bl_scp):

	if options.verbose >= VERBOSE:
		print('Start SCP session (use -v for details)')

	# Get the connection packets
	con_req = packet_list[0]
	con_reply = packet_list[1]
	con_ack = packet_list[2]

	if options.auto_reset:
		if options.verbose >= VERBOSE:
			print('Reset Board throw UART')
		bl_scp.reset()

	if options.verbose >= VERBOSE:
		print('Trying to Connect.')

	bbar = progressbar.ProgressBar(widgets=[progressbar.widgets.AnimatedMarker()], maxval=options.first_retry_nb - 1).start()
	for i in bbar((i for i in range(options.first_retry_nb))):
		try:
			con_req.process(bl_scp)
			con_reply.process(bl_scp)
			break
		except KeyboardInterrupt:
			print("Keyboard Interruption : Closing connection !")
			raise Exception()
		except Exception as inst:
			pass

	if options.mpc:
		mpc_status(2, len(packet_list))

	con_ack.process(bl_scp)
	time.sleep(options.packet_delay/1000)

	if options.verbose >= VERBOSE:
		print('\nConnected !')

	if options.mpc:
		mpc_status(3, len(packet_list))
	current = 0
	if options.verbose <= VERBOSE and not options.mpc:
		bar = progressbar.ProgressBar(maxval=(len(packet_list) - 5),
									  widgets=[progressbar.widgets.Bar('=', '[', ']'), ' ', progressbar.widgets.Percentage()]).start()

	for packet in packet_list[3:-2]:
		try:
			packet.process(bl_scp)
			time.sleep(options.packet_delay/1000)
			current += 1
			if options.verbose <= VERBOSE and not options.mpc:
				bar.update(current)
			if options.mpc:
				mpc_status(current + 3, len(packet_list))
		except Exception as insts:
			raise

	if options.verbose <= VERBOSE and not options.mpc:
		bar.finish()

	if options.verbose >= VERBOSE:
		print('\nDisconnecting...')

	decon_req = packet_list[-2]
	decon_reply = packet_list[-1]
	step=0
	try:
		decon_req.process(bl_scp)
		time.sleep(options.packet_delay/1000)
		if options.mpc:
				mpc_status(current + 4, len(packet_list))
		step = 1
		decon_reply.process(bl_scp)
		if options.mpc:
				mpc_status(current + 5, len(packet_list))
		else:
			print('Disconnected !')
		step = 2
	except Exception as insts:
		# There is known issue for usb, rom code close usb interface before flushing usb socket, 
		# so during disconnection error occurs, pass this error message
		if options.scp_interface == INTERFACE_USB: 
			if options.mpc:
				if step == 0:
					mpc_status(current + 4, len(packet_list))
					mpc_status(current + 5, len(packet_list))
				elif step == 1:
					mpc_status(current + 5, len(packet_list))
			pass
		else:
			raise


# ---- MAIN

if __name__ == "__main__":
	colorama.init()
	return_code = 0

	usage = "usage: " + PROG + " [options] FILENAME"
	version = "%prog " + VERSION + "\n" + COPYRIGHT

	parser = OptionParser(prog=PROG, usage=usage, version=version, epilog=EPILOG)

	parser.add_option("-c", "--chip", dest="chip_name", 
						help="Define the CHIP to communicate, if not defined, default one will be used which defined at sys variables", metavar="CHIP")
	parser.add_option("-s", "--serial", dest="serial", type="string", 
						help="Define the serial port to use")
	parser.add_option("-i", "--interface", dest="scp_interface", type="string", default=INTERFACE_UART,
						help="Define scp bus interface, uart, usb, eth, spi")
	parser.add_option("--list-serial", action="store_true", dest="list_serial", default=False, 
						help="Display available serial ports")
	parser.add_option("-x", "--extra", 	dest="extra_packet", type="string", 
						help="Send existing packet that provided by Maxim")
	parser.add_option("--list-extra-packets", action="store_true", dest="list_extra_packets", default=False, 
						help="List existing scp packets that provided by Maxim for related chip")
	parser.add_option("-v", action="count", dest="verbose", 
						help="Enable verbose mode")
	parser.add_option("-l", "--license", action="store_true", dest="license", default=False, 
						help="Display license")
	parser.add_option("--auto-reset", action="store_true", dest="auto_reset", default=False, 
						help="Perform a reset throw UART RTS before SCP session")
	parser.add_option("-b", "--bl-emulation", action="store_true", dest="bl_emulation", default=False, 
						help="Emulate the bootloader")
	parser.add_option("-m", "--mpc", action="store_true", dest="mpc", default=False, 
						help="Activate mpc standard output")
	parser.add_option("-d", "--dump-file", dest="dump_filename", 
						help="Write dump to FILE", metavar="FILE")

	# Timing Options
	group = OptionGroup(parser, "Timing Options")

	group.add_option("-t", "--timeout", dest="timeout", type="int", default=10, 
						help="Specifies the protocol timeout (s).By default the timeout is 10s")
	group.add_option("-e", "--erase-timeout", dest="erase_timeout", type="int", default=10, 
						help="Specifies the protocol erase mem command timeout (s). By default the timeout is 5s")
	group.add_option("-f", "--first-retry", dest="first_retry_nb", type="int", default=200, 
						help="Specifies the number of retry for first packet. By default the number is 200")
	group.add_option("-r", "--retry", dest="retry_nb", type="int", default=1, 
						help="Specifies the number of retry for packets. By default the number is 1")
	group.add_option("--packet-delay", dest="packet_delay", type="int", default=0, 
						help="Specifies delay between each packet(ms).By default the delay is 0")

	parser.add_option_group(group)

	# Serial Options
	group = OptionGroup(parser, "Serial Options")

	group.add_option("--serial-baudrate", dest="serial_baudrate", type="int", default=115200, 
						help="Specifies the serial baudrate. By default the baudrate is 115200")
	group.add_option("--serial-rtscts", action="store_true", dest="serial_rtscts", default=False, 
						help="Enable serial HW flow control")
	group.add_option("--serial-dsrdtr", action="store_true", dest="serial_dsrdtr", default=False, 
						help="Enable serial HW flow control")
	group.add_option("--serial-xonxoff", action="store_true", dest="serial_xonxoff", default=False, 
						help="Enable serial HW flow control")
	group.add_option("--serial-bytesize", dest="serial_bytesize", type="int", default=8, 
						help="Specifies the serial bytesize. By default the bytesize is 8")
	group.add_option("--serial-stopbits", dest="serial_stopbits", type="string",
					  	help="Specifies the serial number of stop bits. By default the number of stop bits is 2 for MAX32570, 1 for other micros")
	group.add_option("--serial-parity", dest="serial_parity",default="none",
					 	help="Serial parity(none, odd, even, mark, space) default None")

	parser.add_option_group(group)

	(options, args) = parser.parse_args()
	if options.license:
		print(LICENSE)
		sys.exit(0)

	if options.verbose is None:
		options.verbose = 0

	if options.list_serial:
		print("Available serial ports:")
		for port_name in scan():
			print('  - ' + port_name)
		sys.exit(0)

	if options.chip_name is None:
		options.chip_name = os.environ['MAXIM_SBT_DEVICE'] if 'MAXIM_SBT_DEVICE' in os.environ else 'MAX32560'
	# To simplify usage for MAX3259x-NAND/NOR/SPINOR	
	options.chip_name = options.chip_name.split('-')[0]
	
	if options.list_extra_packets:
		list_extra_packets()
		sys.exit(0)

	if options.serial_stopbits is None:
		if options.chip_name in ("MAX32570", "MAX32651", "MAX32666"):
			# these device require 2 stop bits
			options.serial_stopbits = '2'
		else:
			# 1 stop bits for other micros
			options.serial_stopbits = '1'

	if options.dump_filename is not None:
		if os.path.exists(options.dump_filename):
			os.remove(options.dump_filename)

	options.scp_interface = options.scp_interface.lower()
	if options.scp_interface not in (INTERFACE_UART, INTERFACE_USB, INTERFACE_ETH, INTERFACE_SPI):
		options.scp_interface = INTERFACE_UART

	if options.serial_dsrdtr:
		options.scp_interface = INTERFACE_USB # to backward compatibility, serial_dsrdtr only needs for USB.	
	
	if options.scp_interface == INTERFACE_USB:
		options.serial_dsrdtr = True # USB requires serial-dsrdtr parameters

	if options.packet_delay == 0:
		if options.chip_name in ("MAX32651", "MAX32666", "MAX32672", "MAX32690"):
			if options.scp_interface == INTERFACE_UART:
				# these device require a delay between two packet for UART interface
				options.packet_delay = 100
            
	try:

		bl_scp = None

		if options.extra_packet is not None:
			filename = get_extra_packet()
		else:
			if args.__len__() != 1:
				parser.error("argument(s) missing")
				sys.exit(-1)

			filename = args[0]

		if os.path.isdir(filename):
			filename = os.path.join(filename, "packet.list")

		if options.serial is None:
			if os.name == 'nt':
				serial = 'COM1'
			else:
				serial = '/dev/ttyS0'
		else:
			serial = options.serial

		port_name = serial

		if options.verbose >= VERBOSE:
			print('Open serial port: ' + port_name + ' (timeout: ' + str(options.timeout) + 's)')

		packets_list = parse_scpcmd_file(filename, options)

		while True:
			try:
				if sum(1 for item in iter(list_ports.grep(serial))) == 0:
					if not options.mpc:
						print('Waiting for device ' + serial + ' to appears')
				while sum(1 for item in iter(list_ports.grep(serial))) == 0:
					pass
				time.sleep(0.8)
				bl_scp = BootloaderScp(port_name, options)
				process_packet(packets_list, options, bl_scp)

				break
			except FlashCRKWriteError:
				bl_scp.close()
				bl_scp = BootloaderScp(port_name, options)
				packets_list[0].process(bl_scp)
				bl_scp.close()

			except Exception:
				raise
		if not options.mpc:
			print_ok("SCP session OK")
	except ValueError:
		if not options.mpc:
			print_err("Connection Failed")
		return_code = -2
	except Error:
		print("An error Happened, Quiting...")
	except Exception as inst:
		if not options.mpc:
			print(inst)
			print_err("error: SCP session FAILED")
		return_code = -1

	finally:
		if bl_scp is not None:
			bl_scp.close()

	sys.exit(return_code)


