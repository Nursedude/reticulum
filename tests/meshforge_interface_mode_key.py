# MeshForge fork test (config parser): `interface_mode = gateway` and
# `interface_mode = internal` must select those modes. Upstream's
# `interface_mode` branch tested `c["mode"]` for these two values, so a config
# using only the `interface_mode` key raised KeyError('mode') and the
# interface never came up, while `mode = gateway` worked. Every other value
# already read the right key. No wire or crypto change.
import sys
import unittest
from unittest import mock

import configobj

import RNS
from RNS.Interfaces.Interface import Interface


def _mode_for(key, value):
    """Run the real _synthesize_interface over one UDPInterface stanza and
    return the mode it hands to the interface constructor."""
    section = configobj.ConfigObj(["[i]", "type = UDPInterface",
                                   "enabled = yes", f"{key} = {value}"])["i"]
    seen = {}

    def fake_udp(_transport, cfg):
        seen["mode"] = cfg["selected_interface_mode"]
        raise _Stop()

    ret_mod = sys.modules["RNS.Reticulum"]
    assert ret_mod.__file__.startswith(__file__.rsplit("/tests/", 1)[0]), ret_mod.__file__
    with mock.patch.object(ret_mod.UDPInterface, "UDPInterface", fake_udp), \
         mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)), \
         mock.patch.object(RNS, "panic", side_effect=_Panicked("rnsd would exit (RNS.panic)")):
        try:
            RNS.Reticulum._synthesize_interface(mock.MagicMock(), section, "i")
        except _Stop:
            pass
    return seen.get("mode")


class _Stop(BaseException):
    """Ends the run after the constructor; BaseException so the parser's
    own `except Exception` (which calls RNS.panic) does not swallow it."""


class _Panicked(BaseException):
    pass


class MeshForgeInterfaceModeKeyTest(unittest.TestCase):
    def test_interface_mode_gateway(self):
        for v in ("gateway", "gw"):
            self.assertEqual(_mode_for("interface_mode", v), Interface.MODE_GATEWAY, v)

    def test_interface_mode_internal(self):
        self.assertEqual(_mode_for("interface_mode", "internal"), Interface.MODE_INTERNAL)

    def test_both_keys_agree_for_every_mode(self):
        for v, want in (("full", Interface.MODE_FULL),
                        ("access_point", Interface.MODE_ACCESS_POINT),
                        ("roaming", Interface.MODE_ROAMING),
                        ("boundary", Interface.MODE_BOUNDARY),
                        ("gateway", Interface.MODE_GATEWAY),
                        ("internal", Interface.MODE_INTERNAL)):
            self.assertEqual(_mode_for("mode", v), want, f"mode = {v}")
            self.assertEqual(_mode_for("interface_mode", v), want, f"interface_mode = {v}")


if __name__ == "__main__":
    unittest.main()
