# MeshForge fork test (mf.4, Issue #68-class): the rnsd-SIGTERM hang.
#
# Two coupled defects, both reproduced from a real captured stack on moc1
# (2026-05-31 23:09): a logfile-write failure self-deadlocked RNS.log() on a
# non-reentrant logging_lock (Part A), and the SIGTERM/SIGINT handlers ran
# detach_interfaces() — which joins a worker that calls RNS.log() — directly in
# signal context, deadlocking against that held lock (Part B).
import threading
import time
import unittest
from unittest import mock

import RNS
from RNS.Reticulum import Reticulum


class MeshForgeLogReentrancyTest(unittest.TestCase):
    # ---- Part A: logging_lock must be reentrant ----------------------------
    def test_logging_lock_is_reentrant(self):
        # A plain Lock self-deadlocks when log()'s on-write-failure fallback
        # re-calls log() while still holding the lock.
        self.assertEqual(type(RNS.logging_lock).__name__, "RLock")
        # Same-thread double acquire must not block.
        acquired = RNS.logging_lock.acquire(timeout=2)
        self.assertTrue(acquired)
        try:
            self.assertTrue(RNS.logging_lock.acquire(timeout=2))
            RNS.logging_lock.release()
        finally:
            RNS.logging_lock.release()

    def test_log_does_not_deadlock_on_logfile_write_failure(self):
        saved = (RNS.loglevel, RNS.logdest, RNS.logfile,
                 RNS._always_override_destination)
        try:
            RNS.loglevel = RNS.LOG_DEBUG
            RNS.logdest = RNS.LOG_FILE
            RNS.logfile = "/nonexistent_dir_mf4_reentrancy/rns.log"
            done = {}

            def call():
                RNS.log("trigger-mf4-defect-a", RNS.LOG_CRITICAL)
                done["ok"] = True

            th = threading.Thread(target=call, daemon=True)
            th.start()
            th.join(timeout=5)
            self.assertFalse(
                th.is_alive(),
                "log() deadlocked on a logfile-write failure (Part A regressed)",
            )
            self.assertTrue(done.get("ok"))
        finally:
            (RNS.loglevel, RNS.logdest, RNS.logfile,
             RNS._always_override_destination) = saved

    # ---- Part B: signal handlers must defer teardown off signal context ----
    def _reset_shutdown_guard(self):
        setattr(Reticulum, "_Reticulum__shutdown_started", False)

    def test_sigterm_handler_defers_and_returns_promptly(self):
        self._reset_shutdown_guard()
        detached = threading.Event()
        exited = threading.Event()

        def fake_detach():
            detached.set()

        def fake_exit(code=0):
            exited.set()

        with mock.patch.object(RNS.Transport, "detach_interfaces",
                               side_effect=fake_detach), \
                mock.patch.object(RNS, "exit", side_effect=fake_exit):
            t0 = time.time()
            Reticulum.sigterm_handler(15, None)   # must NOT block on teardown
            elapsed = time.time() - t0
            self.assertLess(
                elapsed, 0.5,
                "sigterm_handler blocked in signal context (Part B regressed)",
            )
            # Teardown still runs — just on its own thread.
            self.assertTrue(exited.wait(timeout=5))
            self.assertTrue(detached.is_set())

    def test_sigint_handler_also_defers(self):
        self._reset_shutdown_guard()
        exited = threading.Event()
        with mock.patch.object(RNS.Transport, "detach_interfaces"), \
                mock.patch.object(RNS, "exit", side_effect=lambda code=0: exited.set()):
            Reticulum.sigint_handler(2, None)
            self.assertTrue(exited.wait(timeout=5))

    def test_repeated_signals_spawn_one_shutdown(self):
        self._reset_shutdown_guard()
        calls = []
        with mock.patch.object(RNS.Transport, "detach_interfaces",
                               side_effect=lambda: calls.append(1)), \
                mock.patch.object(RNS, "exit", side_effect=lambda code=0: None):
            Reticulum.sigterm_handler(15, None)
            Reticulum.sigterm_handler(15, None)   # duplicate must be a no-op
            time.sleep(1)
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
