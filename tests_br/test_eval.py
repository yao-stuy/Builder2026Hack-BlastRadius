import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@unittest.skipUnless(os.path.isdir("/kit/sample"), "needs /kit")
class T(unittest.TestCase):
    def test_oracle_total(self):
        import importlib
        ev = importlib.import_module("eval")
        self.assertEqual(f"{ev.oracle_total('/kit/sample'):.3f}", "94.351")


if __name__ == "__main__":
    unittest.main()
