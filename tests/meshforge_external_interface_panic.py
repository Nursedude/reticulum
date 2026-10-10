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
    import configobj
    import RNS
    assert RNS.__file__.startswith({repo!r}), RNS.__file__
    RNS.loglevel = RNS.LOG_DEBUG
    me = mock.MagicMock()
    me.interfacepath = {ipath!r}
    section = configobj.ConfigObj(["[p]", "type = {itype}", "enabled = yes"])["p"]
    with mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)):
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
    # control: an ordinary import error was ALREADY contained upstream
    "RaiseAtImport": """
        raise ImportError("No module named 'meshcore'")
    """,
}


def _run(itype, after=""):
    with tempfile.TemporaryDirectory() as ipath:
        if itype in PLUGINS:
            with open(os.path.join(ipath, f"{itype}.py"), "w") as f:
                f.write(textwrap.dedent(PLUGINS[itype]))
        code = CHILD.format(repo=REPO, ipath=ipath, itype=itype, after=after)
        return subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=60, cwd=ipath)


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
