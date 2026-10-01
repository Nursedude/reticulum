# MeshForge fork test (#69 class): Reticulum(require_shared_instance=True)
# with no shared instance running must refuse AND release @rns/<instance>.
# Upstream raised SystemError but left the listener bound for the life of the
# process, because LocalServerInterface inherited the no-op Interface.detach().
# A host (rnsd) starting meanwhile then joined that dead listener as a client.
#
# Runs as its own process (Reticulum is a singleton). Uses a random instance
# name, so it cannot collide with a real rnsd on the box running it.
import os
import secrets
import shutil
import tempfile
import unittest

import RNS


def _abstract_names(prefix):
    with open("/proc/net/unix") as fh:
        return sorted({l.split()[-1] for l in fh if l.split()[-1].startswith(prefix)})


class MeshForgeRequireSharedReleaseTest(unittest.TestCase):
    def test_refusal_releases_the_listener(self):
        if not os.path.exists("/proc/net/unix"):
            self.skipTest("needs Linux /proc/net/unix")
        name = "mftest" + secrets.token_hex(4)
        cfgdir = tempfile.mkdtemp(prefix="mf-reqshared-")
        self.addCleanup(shutil.rmtree, cfgdir, True)
        with open(os.path.join(cfgdir, "config"), "w") as f:
            f.write(f"[reticulum]\n  share_instance = Yes\n  instance_name = {name}\n"
                    "  enable_transport = No\n[logging]\n  loglevel = 2\n[interfaces]\n")

        self.assertEqual(_abstract_names(f"@rns/{name}"), [], "name already bound")
        with self.assertRaises(SystemError):
            RNS.Reticulum(configdir=cfgdir, require_shared_instance=True)
        self.assertEqual(_abstract_names(f"@rns/{name}"), [],
                         "refused client still holds the shared-instance listener")


if __name__ == "__main__":
    unittest.main()
