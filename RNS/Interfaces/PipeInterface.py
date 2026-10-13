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
import select
import struct
# Unix-only: the gate's byte count needs them. `Reticulum` star-imports every
# interface, so an unguarded import here broke `import RNS` where they do not
# exist (non-author pass on 9120f760, F2); without them the gate keeps the
# free-page poll and counts it (see _pipe_full).
try:
    import fcntl
    import termios
except ImportError:
    fcntl = termios = None

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
    # ...but for at most TX_CONGEST_FULL_S per congestion episode. With
    # `enable_transport = Yes` a forwarded frame is transmitted on the reader
    # thread of the interface it ARRIVED on; a child that drains slower than
    # it is offered keeps completing writes, so the stall check never fires,
    # and a wait on every frame saturated that reader (transport-forwarding
    # drill 2026-10-10: 100 f/s x 20 ms; control traffic on the arriving TCP
    # interface ran 8 s median / 20 s worst). Once puts have waited with
    # the pipe full for TX_CONGEST_FULL_S the interface is congested: frames
    # are offered without waiting (dropped only if the queue is full) until
    # the child has caught up across a window of TX_CONGEST_FULL_S of offers
    # (1.3.8+mf.9: no drop, queue at or under TX_LOW_WATER, pipe under the
    # line for most of the window — see _caught_up_for) — exit needs
    # persistent evidence, as entry does. One caught-up sample was true with the child 1.3 s behind
    # whenever the queue dump fit in the emptied pipe: 502-B frames pack 32
    # to a 16 KB page, so 256 frames fill 8 of 16 pages and select() reads
    # writable, and the gate re-armed every ~1.3 s (fresh pass on b18aae01,
    # F1: 14 episodes / 368 waits / reader duty 0.38 in 20 s vs 6adfa33b 16
    # waits). A child that is behind refills the queue from the offered
    # stream within ~0.2 s of the dump; it cannot keep it empty for 0.5 s.
    # TX_LOW_WATER gates recovery from a stall, so a child that frees a
    # pipe page less often than TX_STALL_S (16 KB pages at 2 kB/s: ~8 s)
    # does not flap Down/Up on every page.
    TX_LOW_WATER = TX_QUEUE_DEPTH // 2
    # A put timeout is evidence of a child that is BEHIND only when the
    # kernel says so — the pipe is not writable (full) — AND has said so
    # across TX_CONGEST_FULL_S of puts that waited or timed out. Three cuts used weaker
    # evidence and each fresh pass broke it: any timeout (94cd084d) congested
    # a healthy child whose writer had not been scheduled; a clock on the
    # writer's write call (a67be362) counted the GIL re-acquire after the
    # syscall (healthy child under GIL hogs: 1739 of 2000 dropped vs 207 on
    # mf.6, pipe writable when sampled) and missed a child behind at a fast
    # rate, because the kernel wakes a pipe writer per consumed PAGE (200 kB/s
    # on 16 KB pages, ~41 kB/s on 4 KB pages: no single write blocked long
    # enough); one pipe-full sample (6adfa33b) congested a healthy child that
    # was merely unscheduled for 20 ms — a `nice 10` child beside 12 busy
    # processes dropped 1487 of 2000, mf.6 0–93 — and so did a 60 ms span of
    # them: that child drained the 2 MB burst in 0.3 s, slower than the
    # senders for the burst's whole length, and still delivered all of it on
    # mf.6 (19–44 waits) because back-pressure throttled the burst. What
    # separates a burst from the overload this cure is for is DURATION: a
    # pipe is full the moment the child is unscheduled, and stays full for
    # half a second only when the child is behind for longer than any burst
    # a 512 KB buffer should smooth. So the reader is held (one wait per
    # TX_PUT_WAIT_S, at most TX_CONGEST_FULL_S per episode) before frames are
    # dropped: far below TX_STALL_S and the 8 s saturation measured on mf.6,
    # far above scheduler latency on a loaded Pi. The pipe is sampled on
    # every put that WAITED for its slot (>= TX_PUT_WAIT_S / 2), not only on
    # one that timed out: a child behind at a rate that frees a page within
    # TX_PUT_WAIT_S (16 KB / 20 ms = 800 kB/s on 16 KB pages, ~200 kB/s on
    # 4 KB pages) lets every put succeed after a wait, so sampling only on
    # timeouts never opened a span and the caller paid a wait per frame as on
    # mf.6 (fresh pass on b18aae01, F2: 0 episodes, reader duty 0.917). A
    # healthy child whose writer is merely slow leaves the pipe writable at
    # those samples, which abandons the span. (mf.7 abandoned on any put
    # that found the pipe writable; since mf.8 the rule is time-weighted and
    # since mf.9 credits only observed waits — see the block below.) A span
    # is abandoned when the time the pipe had room outweighs the time it was
    # backed up, when the writer finds the child caught up (queue empty AND
    # pipe under the line — after a write, or while idle with a span open;
    # "queue at low water" was not that, see _child_caught_up), or when no
    # put has waited or timed out for TX_CONGEST_FULL_S (the caller was being
    # served).
    TX_CONGEST_FULL_S = 0.5
    # 1.3.8+mf.8 (2026-10-12): everything above was built and reviewed on the
    # two Pi 5s (16 KB pages, 256 KB pipe). Seven of the fleet's nine RNS
    # boxes are Pi 3/4: 4 KB pages, a 64 KB pipe, and the mf.7 tag's own tests
    # failed there (moc4 py3.11 + moc5 py3.13, 5/23 then 4/23 then 4/23), run
    # from a clone on the canary BEFORE any install. Two page-size couplings:
    # (1) "pipe full" was select(): writable the moment ONE page is free. On
    #     4 KB pages a child behind at 800 kB/s frees a page every ~5 ms, so
    #     75 % of the samples on a pipe 69-100 % full read writable, no span
    #     formed, and the caller waited 4.3-4.6 s of every 5 s — the mf.6
    #     saturation. The kernel fact that does not depend on the page size is
    #     how many BYTES are buffered (FIONREAD on the write end): the pipe is
    #     backed up when it holds at least TX_PIPE_FULL_FRACTION of its
    #     capacity (F_GETPIPE_SZ). 100 % of the same samples read >= 50 %.
    # (2) the span was abandoned on ANY room sample. A gulp child empties a
    #     64 KB pipe every ~0.3 s and the writer refills it in a few ms; the
    #     puts that waited through the full phase are released in those ms,
    #     so 20-50 room samples land in one 10 ms window against 5 full ones,
    #     and the span never reached TX_CONGEST_FULL_S (68-81 waits vs 56).
    #     Evidence now weighs the TIME it stood for: each sample adds the gap
    #     since the previous one to full or room time; the span congests when
    #     full time reaches TX_CONGEST_FULL_S and is abandoned when room time
    #     exceeds full time (a healthy child under GIL hogs: the pipe has room
    #     for most of the time the caller waits), on a quiet gap, or by the
    #     writer on a caught-up child, as before.
    # A third 4 KB finding was the TEST: a 2000-frame burst into a child that
    # has not reached its first read yet drops frame 321 = 64 + 256 + 1 after
    # TX_PUT_WAIT_S, deterministically; startup latency, not the gate.
    # 1.3.8+mf.9 (2026-10-12, non-author pass on the mf.8 candidate, moc4):
    # (1) mf.8 credited each sample with the WHOLE gap since the previous one,
    #     so the quiet between a healthy child's pulses — pipe empty, nothing
    #     waiting, nothing observed — was booked as full time at the next
    #     pulse's first waited put (0.263 s), and the child congested at
    #     0.54 s with 0 drops, then dropped 1,204 frames (1 of 5 runs). Full
    #     and room time are now credited only with time a put was SEEN
    #     waiting (min of the gap and that put's wait), and the writer looks
    #     at the pipe while idle with a span open, so the quiet is observed.
    # (2) exit required every offer across the window to see the pipe under
    #     the line; a child that keeps up on a timer peaks above it every tick
    #     and the episode never closed. Exit now weighs the window in time,
    #     as entry does (each offer standing for at most TX_PUT_WAIT_S), and
    #     ends it outright on what the domain loses: a dropped frame or a
    #     queue over low water (_caught_up_for).
    TX_PIPE_FULL_FRACTION = 0.5
    F_GETPIPE_SZ = getattr(fcntl, "F_GETPIPE_SZ", 1032)

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
        self.tx_probe_errors = 0
        self._txq = queue.Queue(maxsize=PipeInterface.TX_QUEUE_DEPTH)
        self._tx_last_drain = time.monotonic()
        self._tx_stalled = False
        self._tx_congested = False
        self._tx_full_since = None
        self._tx_full_last = 0.0
        self._tx_full_time = 0.0
        self._tx_room_time = 0.0
        self._tx_clear_since = None
        self._tx_clear_room = 0.0
        self._tx_clear_full = 0.0
        self._tx_offer_last = 0.0
        self._tx_offer_dropped = 0
        self._tx_episode_dropped = 0
        # Serializes the gate's accounting between sender threads and the
        # writer (non-author pass on 9120f760, F5): see _note_pipe_full.
        self._tx_gate_lock = threading.Lock()
        self._pipe_cap = None
        self._pipe_cap_warned = False
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
        if self._tx_congested:
            # The child died mid-episode: the episode ends here, and leaves
            # the same witness as one the child ends by catching up (fresh
            # pass on b18aae01, F3: 2 of 3 respawned episodes had no line).
            RNS.log(f"{self} subprocess respawned mid-episode; {self.tx_dropped - self._tx_episode_dropped} frame(s) dropped during the episode, {self.tx_dropped} total", RNS.LOG_WARNING)
        self._tx_stalled = False
        self._tx_congested = False
        self._tx_full_since = None
        self._tx_clear_since = None
        self._pipe_cap = None   # a new child, a new pipe: re-read its capacity
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
            if self._tx_congested and self._caught_up_for(time.monotonic()):
                self._end_episode()
            # One clock read before the put: the sample below is credited
            # with the time THIS put waited, never more (see _note_pipe_full).
            t = time.monotonic()
            try:
                if self._tx_congested: self._txq.put_nowait(data)
                else:
                    # A put that found the queue full on arrival waited for a
                    # slot however briefly it got one: on 4 KB pages a child
                    # behind at 800 kB/s frees 8 slots every ~5 ms, so the
                    # wait is under TX_PUT_WAIT_S / 2 and a wait-length rule
                    # alone never sampled (4 KB canary, 2026-10-12).
                    throttled = self._txq.full()
                    self._txq.put(data, timeout=PipeInterface.TX_PUT_WAIT_S)
                    now = time.monotonic()
                    # Waited for the slot and got it: the same evidence as a
                    # timed-out put if the pipe is backed up (see TX_CONGEST_FULL_S).
                    if throttled or now - t >= PipeInterface.TX_PUT_WAIT_S / 2: self._note_pipe_full(now, now - t)
            except queue.Full:
                # A drop. While congested the exit rule sees it at the next
                # offer (tx_dropped moved) and restarts its window.
                now = time.monotonic()
                self._note_pipe_full(now, now - t)
                self.tx_dropped += 1
                self._check_tx_stall()
        else:
            # Offered while offline — stalled, a dead child, the respawn
            # window: every refused offer is a drop and is counted, so the
            # recovery and episode lines are honest (fresh pass on b18aae01,
            # F3: only the stalled case counted; 1363 frames vanished in a
            # respawn drill).
            self.tx_dropped += 1

    @property
    def tx_congested(self):
        # Read by Reticulum.get_interface_stats (rnstatus "Dropped" line):
        # in the band this cure is for, dropping is the DESIGNED state, and a
        # designed drop nobody can see is a silent loss (fresh pass on
        # 6adfa33b, F3: tx_dropped had no reader at all).
        return self._tx_congested

    def _note_pipe_full(self, now, waited):
        # Called on a put that waited or timed out, with the time it WAITED.
        # One backed-up sample is not evidence the child is behind (see
        # TX_CONGEST_FULL_S); a span of them is. Evidence is weighed by time,
        # not by sample count (samples cluster in the ms the writer frees
        # slots, 4 KB canary 2026-10-12) — and only by time that was OBSERVED:
        # each sample is credited with the gap since the previous one, but
        # never more than its own put waited. 9120f760 credited the whole gap
        # to the state the pipe was in NOW, so a healthy pulse child's quiet
        # between pulses (pipe empty, nothing waiting) was booked as full:
        # 0.263 s of it at the first waited put of the next pulse, congested
        # at 0.54 s with 0 drops, then 1,204 (non-author pass, F1, moc4).
        # A sender is a thread the gap is real for only while it waits.
        backed_up = self._pipe_full()
        # Serialized (F5): two senders released together both read the
        # previous sample's clock and both credited the gap from it. Under the
        # lock the credits sum to at most the span's wall time. `since` is
        # still read once: the writer clears it (fresh pass on 925d275c, F2).
        with self._tx_gate_lock:
            since = self._tx_full_since
            last = self._tx_full_last
            if since is None or now - last > PipeInterface.TX_CONGEST_FULL_S:
                # No span open, or no put waited for a whole span (the caller was
                # being served): a backed-up sample starts one, a room sample does not.
                if not backed_up:
                    self._tx_full_since = None
                    return
                self._tx_full_time = 0.0
                self._tx_room_time = 0.0
                self._tx_full_since = now
                self._tx_full_last = now
                return
            # A sender that took its clock before another's sample landed
            # arrives with an older `now`: it credits nothing, and the span's
            # clock never moves backward.
            gap = now - last if now > last else 0.0
            credit = min(gap, waited)
            if backed_up: self._tx_full_time += credit
            else: self._tx_room_time += credit
            if now > last: self._tx_full_last = now
            if self._tx_room_time > self._tx_full_time:
                # The pipe had room for longer than it was backed up: the writer,
                # not the child, is what the caller has been waiting on.
                self._tx_full_since = None
                return
            full_for = self._tx_full_time
            if full_for >= PipeInterface.TX_CONGEST_FULL_S and not self._tx_congested:
                self._tx_congested = True
                self._tx_clear_since = None
                self._tx_episode_dropped = self.tx_dropped
                self._tx_offer_dropped = self.tx_dropped
                RNS.log(f"{self} subprocess is behind (callers waited {full_for:.2f}s for a queue slot with its stdin pipe at least {PipeInterface.TX_PIPE_FULL_FRACTION:.0%} full); offering without waiting and dropping what the queue cannot hold until {PipeInterface.TX_CONGEST_FULL_S:.1f}s of offers pass with no drop, the queue at or under {PipeInterface.TX_LOW_WATER} and the pipe under {PipeInterface.TX_PIPE_FULL_FRACTION:.0%} for most of that time", RNS.LOG_WARNING)

    def _end_span(self):
        with self._tx_gate_lock:
            self._tx_full_since = None

    def _end_episode(self):
        with self._tx_gate_lock:
            if not self._tx_congested: return
            self._tx_congested = False
            self._tx_full_since = None
            self._tx_clear_since = None
            RNS.log(f"{self} subprocess has caught up; {self.tx_dropped - self._tx_episode_dropped} frame(s) dropped during the episode, {self.tx_dropped} total", RNS.LOG_WARNING)

    def _caught_up_for(self, now):
        # Called on every offer while congested: True once a window of at
        # least TX_CONGEST_FULL_S of offers has passed with no frame dropped,
        # the queue at or under TX_LOW_WATER at every offer, and the child
        # caught up (queue empty, pipe under the line) for MORE of that time
        # than not — the same weighing, and the same constant, as entry (hfm
        # #5). 9120f760 required EVERY offer in the window to be caught up: a
        # child that keeps up on a timer (reads all of it every 0.25 s) leaves
        # ~39 KB in a 64 KB pipe at the end of each tick at 160 kB/s, ~28 of
        # 160 offers/s landed above the 32 KB line, and the episode never
        # closed — 10 s, drops flat at 175 (non-author pass, F4, moc4). What
        # the domain loses is frames: a drop or a queue over low water ends
        # the window outright; a pipe that is over the line for most of a
        # window is a child still holding a backlog (a gulp child after the
        # queue dump on 16 KB pages), and that window is simply not enough
        # evidence — the next one starts. A gap of more than
        # TX_CONGEST_FULL_S between offers is unobserved time, not caught-up
        # time: the window restarts at the offer after it. With no offers the
        # flag holds; nothing is lost while nothing is offered.
        caught_up = self._child_caught_up()
        with self._tx_gate_lock:
            last_offer = self._tx_offer_last
            self._tx_offer_last = now
            dropped = self.tx_dropped != self._tx_offer_dropped
            self._tx_offer_dropped = self.tx_dropped
            if dropped or self._txq.qsize() > PipeInterface.TX_LOW_WATER:
                self._tx_clear_since = None
                return False
            since = self._tx_clear_since
            if since is None or now - last_offer > PipeInterface.TX_CONGEST_FULL_S:
                self._tx_clear_since = now
                self._tx_clear_room = self._tx_clear_full = 0.0
                return False
            gap = now - last_offer if now > last_offer else 0.0
            # An offer observes one instant: it stands for at most one put
            # wait of the time around it, never for the whole gap since the
            # last offer — a sender descheduled for 0.4 s that offers just
            # after the child read would otherwise book 0.4 s of room it never
            # saw (the F1 class, on the exit side).
            credit = min(gap, PipeInterface.TX_PUT_WAIT_S)
            if caught_up: self._tx_clear_room += credit
            else: self._tx_clear_full += credit
            if now - since < PipeInterface.TX_CONGEST_FULL_S:
                return False
            if self._tx_clear_room > self._tx_clear_full:
                return True
            # A window that was backed up for most of its time: the next one.
            self._tx_clear_since = now
            self._tx_clear_room = self._tx_clear_full = 0.0
            return False

    def _child_caught_up(self):
        # Caught up means NOTHING is backed up: the queue is empty AND the
        # kernel pipe has room. "Queue at low water" was not that: a child
        # behind at a steady rate that reads in gulps (one os.read empties the
        # 256 KB pipe) let the writer dump the whole queue into the emptied
        # pipe — queue empty, pipe full again, child still behind — and the
        # gate re-armed every cycle (fresh pass on 925d275c, F1: 9 episodes in
        # 30 s, reader duty 0.31 vs 0.02). Checked by the writer after a
        # write while a span is open, and by the caller while congested.
        return self._txq.qsize() == 0 and not self._pipe_full()

    def _pipe_full(self):
        # The kernel's own fact, one ioctl per put that waited: the bytes
        # buffered in the child's stdin pipe (FIONREAD works from the write
        # end on Linux), against the pipe's capacity (F_GETPIPE_SZ, read once
        # per child). Backed up = at least TX_PIPE_FULL_FRACTION of it. Not a
        # free-page poll: select() reads writable whenever ONE page is free,
        # and how often that is depends on the page size (4 KB canary,
        # 2026-10-12: 75 % writable on a pipe 69-100 % full). Where the
        # capacity cannot be read (not Linux) the free-page poll is kept.
        # Unobservable reads as NOT full: without evidence the interface keeps
        # mf.6 back-pressure. Every error shape (process None, stdin
        # None/closed, dead child, non-pipe fd) lands here (fresh pass on
        # 6adfa33b, F5) — counted so a probe that is permanently broken is
        # not permanently invisible.
        try:
            fd = self.process.stdin.fileno()
            cap = self._pipe_cap
            if cap is None:
                try:
                    if fcntl is None: raise OSError("fcntl/termios are not available on this platform")
                    cap = fcntl.fcntl(fd, PipeInterface.F_GETPIPE_SZ)
                except OSError as e:
                    # Counted and named (non-author pass on 9120f760, F3): the
                    # fallback is the signal this arc measured WRONG on 4 KB
                    # pages, so it must never be silent.
                    cap = 0
                    self.tx_probe_errors += 1   # capacity unreadable
                    if not self._pipe_cap_warned:
                        self._pipe_cap_warned = True
                        RNS.log(f"{self} cannot read its stdin pipe capacity ({e}); the congestion gate falls back to a select() free-page poll, which reads a pipe on 4 KB pages writable while it is backed up", RNS.LOG_WARNING)
                self._pipe_cap = cap
            if cap <= 0:
                return not select.select([], [fd], [], 0)[1]
            buffered = struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
            return buffered >= cap * PipeInterface.TX_PIPE_FULL_FRACTION
        except Exception as e:
            self.tx_probe_errors += 1
            if self.tx_probe_errors == 1:
                RNS.log(f"{self} could not read its stdin pipe state ({e}); treating the pipe as not full", RNS.LOG_DEBUG)
            return False

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
                # While a pipe-full span is open the writer looks at the pipe
                # whenever it is idle, so the quiet after a burst is OBSERVED:
                # the caught-up check after a write sees the pipe the burst's
                # last write left over the line, and nothing re-checked it
                # (non-author pass on 9120f760, F1: `occ=0 qsize=0` between
                # pulses with the span still open).
                data = self._txq.get(timeout=PipeInterface.TX_PUT_WAIT_S if self._tx_full_since is not None else 0.5)
            except queue.Empty:
                if self._tx_full_since is not None:
                    if self._child_caught_up(): self._end_span()
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
            # The burst was absorbed: a pipe-full span that began before it
            # does not carry into the next one.
            if self._tx_full_since is not None and self._child_caught_up():
                self._end_span()
            if written != len(data):
                RNS.log("Pipe interface only wrote "+str(written)+" bytes of "+str(len(data)), RNS.LOG_ERROR)
            if (self._tx_stalled and gen == self._writer_gen and self.pipe_is_open and not self.detached
                    and self._txq.qsize() <= PipeInterface.TX_LOW_WATER):
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
