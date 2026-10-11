# Reticulum License
#
# Copyright (c) 2016-2025 Mark Qvist
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# - The Software shall not be used in any kind of system which includes amongst
#   its functions the ability to purposefully do harm to human beings.
#
# - The Software shall not be used, directly or indirectly, in the creation of
#   an artificial intelligence, machine learning or language model training
#   dataset, including but not limited to any use that contributes to the
#   training or development of such a model or algorithm.
#
# - The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from RNS.Interfaces.Interface import Interface
from time import sleep
import sys
import threading
import time
import RNS

import subprocess
import shlex
import queue

class HDLC():
    # The Pipe Interface packetizes data using
    # simplified HDLC framing, similar to PPP
    FLAG              = 0x7E
    ESC               = 0x7D
    ESC_MASK          = 0x20

    @staticmethod
    def escape(data):
        data = data.replace(bytes([HDLC.ESC]), bytes([HDLC.ESC, HDLC.ESC^HDLC.ESC_MASK]))
        data = data.replace(bytes([HDLC.FLAG]), bytes([HDLC.ESC, HDLC.FLAG^HDLC.ESC_MASK]))
        return data

class PipeInterface(Interface):
    MAX_CHUNK = 32768
    BITRATE_GUESS = 1*1000*1000
    DEFAULT_IFAC_SIZE = 8

    # MeshForge fork: outbound frames go through a bounded queue drained by a
    # writer thread, never a blocking stdin.write() on the caller's thread
    # (Transport.transmit). A child that stops draining costs THIS interface:
    # after TX_STALL_S without a completed write the interface reads offline
    # (rnstatus: Down), frames are dropped and counted, and it comes back
    # the moment the child drains again. Bench drill 2026-10-10: stock blocked
    # the sending thread 47 s on a SIGSTOPped child, every other interface on
    # that thread starved, `online` True throughout.
    TX_QUEUE_DEPTH = 256
    TX_STALL_S = 5.0
    # A full queue with a child that is still draining is back-pressure, not
    # a stall: the caller waits at most this long for a slot before the frame
    # is dropped (fresh pass F2: a 64-deep queue dropped frames a healthy
    # child would have taken, because the writer had not had the GIL yet).
    TX_PUT_WAIT_S = 0.02

    owner    = None
    command  = None
    
    def __init__(self, owner, configuration):
        super().__init__()

        c = Interface.get_config_obj(configuration)
        name = c["name"]
        command = c["command"] if "command" in c else None
        respawn_delay = c.as_float("respawn_delay") if "respawn_delay" in c else None

        if command == None:
            raise ValueError("No command specified for PipeInterface")

        if respawn_delay == None:
            respawn_delay = 5

        self.HW_MTU = 1064
        
        self.owner    = owner
        self.name     = name
        self.command  = command
        self.process  = None
        self.timeout  = 100
        self.online   = False
        self.pipe_is_open = False
        self.bitrate  = PipeInterface.BITRATE_GUESS
        self.respawn_delay = respawn_delay
        self.tx_dropped = 0
        self._txq = queue.Queue(maxsize=PipeInterface.TX_QUEUE_DEPTH)
        self._tx_last_drain = time.monotonic()
        self._tx_stalled = False
        self._writer_gen = 0
        self._writer = None

        try:
            self.open_pipe()

        except Exception as e:
            RNS.log("Could connect pipe for interface "+str(self), RNS.LOG_ERROR)
            raise e

        if self.pipe_is_open:
            self.configure_pipe()
        else:
            raise IOError("Could not connect pipe")


    def open_pipe(self):
        RNS.log("Connecting subprocess pipe for "+str(self)+"...", RNS.LOG_VERBOSE)
        
        try:
            self.process = subprocess.Popen(shlex.split(self.command), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            self.pipe_is_open = True
        except Exception as e:
            raise e
            self.pipe_is_open = False


    def configure_pipe(self):
        sleep(0.01)
        thread = threading.Thread(target=self.readLoop)
        thread.daemon = True
        thread.start()
        self._writer_gen += 1
        # Retire the previous child's writer BEFORE the new one starts: a
        # writer still parked on the shared queue would take the next frame,
        # break on the dead child's pipe and mark the LIVE interface offline
        # (fresh pass F1, 18/20 respawns).
        if self._writer is not None and self._writer.is_alive():
            self._writer.join(1.0)
        self._tx_last_drain = time.monotonic()
        self._tx_stalled = False
        self._writer = threading.Thread(target=self._write_loop, args=(self._writer_gen, self.process), daemon=True,
                                        name=f"pipe-writer-{self.name}")
        self._writer.start()
        self.online = True
        RNS.log("Subprocess pipe for "+str(self)+" is now connected", RNS.LOG_VERBOSE)


    def process_incoming(self, data):
        self.rxb += len(data)            
        self.owner.inbound(data, self)


    def process_outgoing(self,data):
        if self.online:
            data = bytes([HDLC.FLAG])+HDLC.escape(data)+bytes([HDLC.FLAG])
            try:
                self._txq.put(data, timeout=PipeInterface.TX_PUT_WAIT_S)
            except queue.Full:
                self.tx_dropped += 1
                self._check_tx_stall()
        elif self._tx_stalled:
            # Offered while stalled: counted, so the recovery line is honest.
            self.tx_dropped += 1

    def _check_tx_stall(self):
        # Called when the queue is full: the writer is wedged in a write the
        # child is not draining. Measured, not assumed — the clock starts at
        # the last COMPLETED write.
        stalled_for = time.monotonic() - self._tx_last_drain
        if not self._tx_stalled and stalled_for >= PipeInterface.TX_STALL_S:
            self._tx_stalled = True
            self.online = False
            RNS.log(f"{self} subprocess has not drained its stdin for {stalled_for:.1f}s; interface is now offline, {self.tx_dropped} frame(s) dropped so far", RNS.LOG_WARNING)

    def _write_loop(self, gen, process):
        while gen == self._writer_gen and not self.detached:
            try:
                data = self._txq.get(timeout=0.5)
            except queue.Empty:
                continue
            if gen != self._writer_gen or self.detached:
                # Retired while waiting: hand the frame back for the live
                # writer; never write it to this generation's child.
                try: self._txq.put_nowait(data)
                except queue.Full: pass
                return
            try:
                written = process.stdin.write(data)
                process.stdin.flush()
            except Exception as e:
                RNS.log(f"{self} write to subprocess failed: {e}", RNS.LOG_ERROR)
                if gen == self._writer_gen:
                    self.online = False
                return
            self.txb += len(data)
            self._tx_last_drain = time.monotonic()
            if written != len(data):
                RNS.log("Pipe interface only wrote "+str(written)+" bytes of "+str(len(data)), RNS.LOG_ERROR)
            if self._tx_stalled and gen == self._writer_gen and self.pipe_is_open and not self.detached:
                self._tx_stalled = False
                self.online = True
                RNS.log(f"{self} subprocess is draining again; interface is back online ({self.tx_dropped} frame(s) dropped during the stall)", RNS.LOG_WARNING)


    def readLoop(self):
        try:
            in_frame = False
            escape = False
            data_buffer = b""
            last_read_ms = int(time.time()*1000)

            while True:
                process_output = self.process.stdout.read(1)
                if len(process_output) == 0 and self.process.poll() is not None:
                    break

                else:
                    byte = ord(process_output)
                    last_read_ms = int(time.time()*1000)

                    if (in_frame and byte == HDLC.FLAG):
                        in_frame = False
                        self.process_incoming(data_buffer)
                    elif (byte == HDLC.FLAG):
                        in_frame = True
                        data_buffer = b""
                    elif (in_frame and len(data_buffer) < self.HW_MTU):
                        if (byte == HDLC.ESC):
                            escape = True
                        else:
                            if (escape):
                                if (byte == HDLC.FLAG ^ HDLC.ESC_MASK):
                                    byte = HDLC.FLAG
                                if (byte == HDLC.ESC  ^ HDLC.ESC_MASK):
                                    byte = HDLC.ESC
                                escape = False
                            data_buffer = data_buffer+bytes([byte])

            RNS.log("Subprocess terminated on "+str(self))
            self.process.kill()
                    
        except Exception as e:
            self.online = False
            try:
                self.process.kill()
            except Exception as e:
                pass

            RNS.log("A pipe error occurred, the contained exception was: "+str(e), RNS.LOG_ERROR)
            RNS.log("The interface "+str(self)+" experienced an unrecoverable error and is now offline.", RNS.LOG_ERROR)
            
            if RNS.Reticulum.panic_on_interface_error:
                RNS.panic()

            RNS.log("Reticulum will attempt to reconnect the interface periodically.", RNS.LOG_ERROR)

        self.online = False
        self.reconnect_pipe()

    def reconnect_pipe(self):
        while not self.online:
            try:
                time.sleep(self.respawn_delay)
                RNS.log("Attempting to respawn subprocess for "+str(self)+"...", RNS.LOG_VERBOSE)
                self.open_pipe()
                if self.pipe_is_open:
                    self.configure_pipe()
            except Exception as e:
                RNS.log("Error while spawning subprocess, the contained exception was: "+str(e), RNS.LOG_ERROR)

        RNS.log("Reconnected pipe for "+str(self))

    def __str__(self):
        return "PipeInterface["+self.name+"]"
