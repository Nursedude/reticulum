# MeshForge fork test: the epoll loop's accept handler in BackboneInterface
# must bind the exception it logs. Upstream used a bare `except:` that logged
# `{e}` with `e` unbound, so a failed accept() (e.g. racing a listener close)
# raised UnboundLocalError inside the handler and killed the shared epoll I/O
# thread for every Backbone/Local interface in the process. Found by both
# reviewers of the require_shared_instance @rns-release fix (2026-10-01).
import ast
import os
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "RNS", "Interfaces", "BackboneInterface.py")


class MeshForgeBackboneAcceptExceptTest(unittest.TestCase):
    def test_no_handler_logs_an_unbound_exception(self):
        tree = ast.parse(open(SRC).read())
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.name is None:
                body_names = {n.id for b in node.body for n in ast.walk(b) if isinstance(n, ast.Name)}
                if "e" in body_names:
                    offenders.append(node.lineno)
        self.assertEqual(offenders, [], f"handlers use `e` without binding it: lines {offenders}")


if __name__ == "__main__":
    unittest.main()
