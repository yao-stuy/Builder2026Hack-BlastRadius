import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br import cmptypes, maps  # noqa: E402

TYPES = [["builtins:NoneType", 1], ["m:Plain", 1], ["m:Custom", 0], ["m:Color", 3], ["builtins:int", 1]]
OLD = "def f(a, b):\n    return a is b\n"
NEW = "def f(a, b):\n    return a == b\n"
PATH = "sqlglot/x.py"
KEY = "2:11:2:17"


def prof(pairs):
    return {"v": 1, "types": TYPES, "sites": {PATH: {KEY: pairs}}}


class T(unittest.TestCase):
    def setUp(self):
        self._k, self._t = maps.known_file, maps.tests_for
        maps.known_file = lambda p: True
        maps.tests_for = lambda p, l, k="any": frozenset()

    def tearDown(self):
        maps.known_file, maps.tests_for = self._k, self._t
        cmptypes._PROFILE = None

    def ok(self, pairs, old=OLD, new=NEW):
        cmptypes._PROFILE = prof(pairs)
        return cmptypes.provably_equivalent(PATH, old, new)

    def test_none_rules(self):
        self.assertTrue(self.ok([[0, 0]]))
        self.assertTrue(self.ok([[0, 1], [1, 0], [0, 4]]))
        self.assertFalse(self.ok([[0, 2]]))
        self.assertFalse(self.ok([[1, 1]]))
        self.assertFalse(self.ok([[0, 1], [4, 4]]))

    def test_enum_rule(self):
        self.assertTrue(self.ok([[3, 3]]))
        self.assertFalse(self.ok([[3, 1]]))
        self.assertFalse(self.ok([[3, 4]]))

    def test_other_diffs_rejected(self):
        self.assertFalse(self.ok([[0, 0]], new="def f(a, b):\n    return a == c\n"))
        self.assertFalse(self.ok([[0, 0]], new="def f(a, b):\n    return a < b\n"))
        self.assertFalse(self.ok([[0, 0]], old=OLD, new=OLD))
        self.assertFalse(self.ok([[0, 0]], new="def f(a, b):\n    return a is not b\n"))
        self.assertFalse(self.ok([[0, 0]], new="def f(a, b:\n"))

    def test_chained_not_touched(self):
        o = "def f(a, b, c):\n    return a is b is c\n"
        n = "def f(a, b, c):\n    return a == b is c\n"
        self.assertFalse(self.ok([[0, 0]], old=o, new=n))

    def test_unprofiled_site(self):
        cmptypes._PROFILE = {"v": 1, "types": TYPES, "sites": {}}
        self.assertTrue(cmptypes.provably_equivalent(PATH, OLD, NEW))
        maps.tests_for = lambda p, l, k="any": frozenset({3})
        self.assertFalse(cmptypes.provably_equivalent(PATH, OLD, NEW))
        self.assertFalse(cmptypes.provably_equivalent("tests/x.py", OLD, NEW))
        self.assertIn("not proved", cmptypes.explain(PATH, OLD, NEW))


if __name__ == "__main__":
    unittest.main()
