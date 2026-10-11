# MeshForge fork test (external interface loading): a user-supplied interface
# module that calls RNS.panic() or sys.exit() while it loads must cost THAT
# interface, not the whole rnsd. Upstream wraps the exec + constructor in
# `except Exception`, which contains an ordinary error but not RNS.panic()
# (os._exit(255)) or SystemExit — so one plugin took down every interface on
# the shared instance, RNode leg included. Measured shape: SMCI
# (Reticulum-Smart-MeshCore-Interface @5038e023fa) calls RNS.panic() from
# __init__ when `import meshcore` fails. A panic OUTSIDE external loading must
# still exit 255. No wire or crypto change.
#
# Each case runs in a CHILD process: the real RNS.panic() is os._exit(255),
# which no in-process mock can be trusted to stand in for.
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

CHILD = textwrap.dedent("""
    import sys
    sys.path.insert(0, {repo!r})
    from unittest import mock
    from RNS.vendor import configobj
    import RNS
    assert RNS.__file__.startswith({repo!r}), RNS.__file__
    RNS.loglevel = RNS.LOG_DEBUG
    me = mock.MagicMock()
    me.interfacepath = {ipath!r}
    section = configobj.ConfigObj(["[p]", "type = {itype}", "enabled = yes"])["p"]
    # Interface.__init__ reads its defaults from the running instance.
    with mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)), \
         mock.patch.object(RNS.Reticulum, "get_instance", staticmethod(lambda: me)):
        RNS.Reticulum._synthesize_interface(me, section, "p")
    print("SURVIVED", flush=True)
    {after}
""")

PLUGINS = {
    # SMCI's shape: the library check panics from __init__
    "PanicInInit": """
        class PanicInInit(Interface):
            def __init__(self, owner, configuration):
                RNS.log("PanicInInit: library missing -- cannot continue", RNS.LOG_CRITICAL)
                RNS.panic()
        interface_class = PanicInInit
    """,
    "PanicAtImport": """
        RNS.panic()
        interface_class = None
    """,
    "ExitInInit": """
        import sys
        class ExitInInit(Interface):
            def __init__(self, owner, configuration):
                sys.exit(3)
        interface_class = ExitInInit
    """,
    # Review 2026-10-10 F1: interface_post_init REGISTERS the interface
    # (Transport.add_interface) BEFORE final_init(), so a panic there used to
    # leave a dead interface registered while the log said "Continuing
    # without it" (rnstatus lists it; path requests are forwarded to it).
    "PanicInFinalInit": """
        class PanicInFinalInit(Interface):
            DEFAULT_IFAC_SIZE = 16
            def __init__(self, owner, configuration):
                super().__init__()
            def final_init(self):
                RNS.panic()
            def detach(self):
                print("DETACHED", flush=True)
        interface_class = PanicInFinalInit
    """,
    # Review 2026-10-10 F2: a plugin's OWN `except Exception` must not turn a
    # panic into a healthy load (before the fork the process exited; a panic
    # that is caught and ignored would load the interface, logging nothing).
    "SwallowPanic": """
        class SwallowPanic(Interface):
            DEFAULT_IFAC_SIZE = 16
            def __init__(self, owner, configuration):
                super().__init__()
                try:
                    RNS.panic()
                except Exception:
                    pass
        interface_class = SwallowPanic
    """,
    # The refusal detaches what it unregisters (F1). A plugin whose detach()
    # never returns must not turn "refused" into "rnsd hangs at startup" --
    # systemd restarts a crash, not a hang. Bounded by RNS_DETACH_TIMEOUT.
    "BlockingDetach": """
        import threading
        class BlockingDetach(Interface):
            DEFAULT_IFAC_SIZE = 16
            def __init__(self, owner, configuration):
                super().__init__()
            def final_init(self):
                RNS.panic()
            def detach(self):
                threading.Event().wait()
        interface_class = BlockingDetach
    """,
    # Review 2026-10-10 (fresh pass) F1: the refusal detaches on a fresh
    # thread; the guard is thread-local, so a panic() in detach() used to run
    # with the guard OFF and os._exit(255) rnsd from the containment's own
    # cleanup.
    "PanicInDetach": """
        class PanicInDetach(Interface):
            DEFAULT_IFAC_SIZE = 16
            def __init__(self, owner, configuration):
                super().__init__()
            def final_init(self):
                RNS.panic()
            def detach(self):
                RNS.panic()
        interface_class = PanicInDetach
    """,
    # F2: a detach() raising SystemExit was caught by nothing on that thread
    # and left no RNS log line -- a swallow without a witness.
    "ExitInDetach": """
        import sys
        class ExitInDetach(Interface):
            DEFAULT_IFAC_SIZE = 16
            def __init__(self, owner, configuration):
                super().__init__()
            def final_init(self):
                RNS.panic()
            def detach(self):
                sys.exit(7)
        interface_class = ExitInDetach
    """,
    # F4: a plugin's bare `except:` catches even a BaseException, so the
    # panic was swallowed and the interface loaded as healthy, silently.
    "BareExceptSwallow": """
        class BareExceptSwallow(Interface):
            DEFAULT_IFAC_SIZE = 16
            def __init__(self, owner, configuration):
                super().__init__()
                try:
                    RNS.panic()
                except:
                    pass
        interface_class = BareExceptSwallow
    """,
    # Review 2026-10-10 (third pass) #1: the swallowed-panic check ran AFTER
    # interface_post_init, so a plugin that swallowed its panic in __init__
    # was registered and had final_init() (threads, ports) RUN before the
    # refusal, relying on the broken plugin's own detach() to undo it.
    "SwallowThenFinalInit": """
        class SwallowThenFinalInit(Interface):
            DEFAULT_IFAC_SIZE = 16
            def __init__(self, owner, configuration):
                super().__init__()
                try:
                    RNS.panic()
                except:
                    pass
            def final_init(self):
                print("FINAL_INIT_RAN", flush=True)
        interface_class = SwallowThenFinalInit
    """,
    # Fourth pass F1: a plugin that swallows its panic and THEN raises an
    # ordinary error was refused, but logged only "initialisation failed" --
    # the swallowed panic left no witness, so the operator read a broken
    # plugin as a plain import error.
    "SwallowThenRaise": """
        class SwallowThenRaise(Interface):
            DEFAULT_IFAC_SIZE = 16
            def __init__(self, owner, configuration):
                super().__init__()
                try:
                    RNS.panic()
                except:
                    pass
                raise RuntimeError("after the swallow")
        interface_class = SwallowThenRaise
    """,
    # Fourth pass F1 (second shape): a panic swallowed at IMPORT time by a
    # module that never defines `interface_class` surfaced as the KeyError,
    # never as a swallowed panic.
    "SwallowAtImportNoClass": """
        try:
            RNS.panic()
        except:
            pass
    """,
    # Fourth pass F3: a panic swallowed at import time still CONSTRUCTED the
    # plugin (ports, threads) before the refusal; the check belongs right
    # after exec, before the constructor runs.
    "SwallowAtImportThenConstruct": """
        try:
            RNS.panic()
        except:
            pass
        class SwallowAtImportThenConstruct(Interface):
            DEFAULT_IFAC_SIZE = 16
            def __init__(self, owner, configuration):
                print("CONSTRUCTED", flush=True)
                super().__init__()
        interface_class = SwallowAtImportThenConstruct
    """,
    # control: an ordinary import error was ALREADY contained upstream
    "RaiseAtImport": """
        raise ImportError("No module named 'meshcore'")
    """,
}


def _run(itype, after="", env=None):
    with tempfile.TemporaryDirectory() as ipath:
        if itype in PLUGINS:
            with open(os.path.join(ipath, f"{itype}.py"), "w") as f:
                f.write(textwrap.dedent(PLUGINS[itype]))
        code = CHILD.format(repo=REPO, ipath=ipath, itype=itype, after=after)
        return subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=60, cwd=ipath,
                              env={**os.environ, **(env or {})})


class MeshForgeExternalInterfacePanicTest(unittest.TestCase):
    def _assert_contained(self, itype):
        r = _run(itype)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 255, f"{itype}: rnsd would exit (RNS.panic)\n{out}")
        self.assertEqual(r.returncode, 0, f"{itype}: child died rc={r.returncode}\n{out}")
        self.assertIn("SURVIVED", r.stdout, out)
        self.assertIn(f"External interface initialisation failed for {itype}", out, out)
        return out

    def test_panic_in_init_costs_only_that_interface(self):
        out = self._assert_contained("PanicInInit")
        self.assertIn("refused", out, out)       # the log names what happened

    def test_panic_at_import_costs_only_that_interface(self):
        self._assert_contained("PanicAtImport")

    def test_sys_exit_in_init_costs_only_that_interface(self):
        self._assert_contained("ExitInInit")

    def test_ordinary_import_error_still_contained(self):
        self._assert_contained("RaiseAtImport")

    def test_panic_in_final_init_leaves_nothing_registered(self):
        r = _run("PanicInFinalInit",
                 after='print("REGISTERED", len(RNS.Transport.interfaces), flush=True)')
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("refused", out, out)
        self.assertIn("REGISTERED 0", r.stdout, out)   # not a zombie
        self.assertIn("DETACHED", r.stdout, out)       # its resources released

    def test_plugin_catch_all_cannot_swallow_a_panic(self):
        r = _run("SwallowPanic",
                 after='print("REGISTERED", len(RNS.Transport.interfaces), flush=True)')
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("refused", out, out)
        self.assertIn("REGISTERED 0", r.stdout, out)

    def test_refusal_does_not_hang_on_a_blocking_detach(self):
        r = _run("BlockingDetach", env={"RNS_DETACH_TIMEOUT": "0.5"},
                 after='print("REGISTERED", len(RNS.Transport.interfaces), flush=True)')
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("SURVIVED", r.stdout, out)
        self.assertIn("REGISTERED 0", r.stdout, out)
        self.assertIn("did not finish detaching", out, out)

    def _assert_refused_cleanly(self, itype, env=None):
        r = _run(itype, env=env,
                 after='print("REGISTERED", len(RNS.Transport.interfaces), flush=True)')
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 255, f"{itype}: rnsd would exit (RNS.panic)\n{out}")
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("SURVIVED", r.stdout, out)
        self.assertIn("refused", out, out)
        self.assertIn("REGISTERED 0", r.stdout, out)
        return out

    def test_panic_in_detach_of_a_refused_interface_is_contained(self):
        out = self._assert_refused_cleanly("PanicInDetach")
        self.assertIn("Detaching refused interface PanicInDetach", out, out)

    def test_sys_exit_in_detach_of_a_refused_interface_leaves_a_witness(self):
        out = self._assert_refused_cleanly("ExitInDetach")
        self.assertIn("Detaching refused interface ExitInDetach", out, out)
        self.assertIn("SystemExit", out, out)

    def test_plugin_bare_except_cannot_swallow_a_panic(self):
        out = self._assert_refused_cleanly("BareExceptSwallow")
        self.assertIn("swallowed", out, out)

    def test_swallowed_panic_in_init_is_refused_before_registration(self):
        out = self._assert_refused_cleanly("SwallowThenFinalInit")
        self.assertIn("swallowed", out, out)
        self.assertNotIn("FINAL_INIT_RAN", out, out)   # never brought up

    def test_swallow_then_ordinary_raise_is_logged_as_a_swallowed_panic(self):
        out = self._assert_refused_cleanly("SwallowThenRaise")
        self.assertIn("swallowed", out, out)

    def test_swallow_at_import_without_interface_class_is_logged_as_a_swallowed_panic(self):
        out = self._assert_refused_cleanly("SwallowAtImportNoClass")
        self.assertIn("swallowed", out, out)

    def test_swallowed_panic_at_import_is_refused_before_construction(self):
        out = self._assert_refused_cleanly("SwallowAtImportThenConstruct")
        self.assertIn("swallowed", out, out)
        self.assertNotIn("CONSTRUCTED", out, out)   # never constructed

    def test_infinite_detach_timeout_does_not_turn_a_refusal_into_an_exit(self):
        for raw in ("inf", "nan"):
            with self.subTest(RNS_DETACH_TIMEOUT=raw):
                self._assert_refused_cleanly("PanicInFinalInit", env={"RNS_DETACH_TIMEOUT": raw})

    def test_unparseable_detach_timeout_does_not_break_import(self):
        r = _run("PanicInInit", env={"RNS_DETACH_TIMEOUT": "abc"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SURVIVED", r.stdout, r.stdout + r.stderr)

    def test_panic_outside_external_loading_still_exits(self):
        r = _run("NoSuchInterface", after="RNS.panic()")
        self.assertIn("SURVIVED", r.stdout, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 255, r.stdout + r.stderr)

    def test_panic_after_a_contained_panic_still_exits(self):
        """The containment is scoped to the load: it must not leak and
        neuter a later, legitimate panic."""
        r = _run("PanicInInit", after="RNS.panic()")
        self.assertIn("SURVIVED", r.stdout, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 255, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
