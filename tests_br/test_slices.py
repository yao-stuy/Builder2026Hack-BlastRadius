"""Unit tests for br.slices (needs sqlglot's tests package: SQLGLOT_ROOT, default /src).
Run: python -m unittest tests_br.test_slices   (in the lab container)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from br import slices  # noqa: E402

ROOT = os.environ.get("SQLGLOT_ROOT", "/src")

PAIRS_OLD = "-- c\nSELECT 1;\nSELECT 1;\n# dialect: x\nSELECT 2;\nSELECT 2;\nSELECT 3;\nSELECT 3;\n"


class Parse(unittest.TestCase):
    def test_fixture_arg(self):
        self.assertEqual(slices.fixture_arg("tests/fixtures/optimizer/simplify.sql"), "optimizer/simplify.sql")
        self.assertEqual(slices.fixture_arg("tests\\fixtures\\identity.sql"), "identity.sql")
        self.assertIsNone(slices.fixture_arg("sqlglot/parser.py"))
        self.assertIsNone(slices.fixture_arg("tests/fixtures/jsonpath/cts.json"))

    def test_changed_pairs(self):
        new = PAIRS_OLD.replace("SELECT 2;\nSELECT 2;", "SELECT 2;\nSELECT 9;")
        self.assertEqual(slices.changed_entries("load_sql_fixture_pairs", PAIRS_OLD, new), {1})

    def test_shift_by_semicolon(self):
        # adding a ';' re-pairs everything after it
        new = PAIRS_OLD.replace("SELECT 1;\nSELECT 1;", "SELECT 1;;\nSELECT 1;")
        r = slices.changed_entries("load_sql_fixture_pairs", PAIRS_OLD, new)
        self.assertTrue({0, 1, 2} <= r)

    def test_removal_and_bad(self):
        self.assertIsNone(slices.changed_entries("load_sql_fixture_pairs", PAIRS_OLD, "SELECT 1;\nSELECT 1;"))
        self.assertIsNone(slices.changed_entries("load_sql_fixture_pairs", PAIRS_OLD, "#nocolon\nSELECT 1;\nx;"))
        self.assertIsNone(slices.changed_entries("nope", "a", "b"))

    def test_fixtures(self):
        old = "a\n# c\nb\nc\n"
        self.assertEqual(slices.parse_fixtures(old), ["a", "# c\nb", "c"])
        self.assertEqual(slices.changed_entries("load_sql_fixtures", old, "a\n# c\nb\nd\nc\n"), {2})
        self.assertEqual(slices.changed_entries("load_sql_fixtures", old, "a\n# c\nB\nc\n"), {1})


@unittest.skipUnless(os.path.isdir(os.path.join(ROOT, "tests")), "no sqlglot checkout")
class Filter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from br import harness
        harness.setup(ROOT)
        slices.install()
        import tests.helpers as h
        cls.h = h

    def tearDown(self):
        slices.clear()

    def test_matches_real_parser(self):
        h = self.h
        for fn, parse, name in ((h.load_sql_fixture_pairs, slices.parse_pairs, "optimizer/simplify.sql"),
                                (h.load_sql_fixtures, slices.parse_fixtures, "identity.sql")):
            with open(os.path.join(h.FIXTURES_DIR, name), encoding="utf-8") as f:
                text = f.read()
            self.assertEqual([list(x) if isinstance(x, tuple) else x for x in fn(name)],
                             [list(x) if isinstance(x, tuple) else x for x in parse(text)])

    def test_filter_and_reference_identity(self):
        import tests.helpers as h
        from tests.helpers import load_sql_fixtures  # name imported before the filter is set
        full = list(load_sql_fixtures("identity.sql"))
        slices.set_filter({"identity.sql": {1, 5}})
        self.assertEqual(list(load_sql_fixtures("identity.sql")), [full[1], full[5]])
        self.assertEqual(len(list(h.load_sql_fixtures("pretty.sql"))) > 2, True)  # other file untouched
        slices.set_filter({"identity.sql": set()}, default_empty=True)
        self.assertEqual(list(load_sql_fixtures("pretty.sql")), [])
        slices.clear()
        self.assertEqual(list(load_sql_fixtures("identity.sql")), full)

    def test_seen(self):
        from tests.helpers import load_sql_fixture_pairs
        slices.reset_seen()
        slices.set_phase("test")
        slices.set_filter({"optimizer/simplify.sql": {0}})
        got = list(load_sql_fixture_pairs("optimizer/simplify.sql"))
        self.assertEqual(len(got), 1)
        (k, (n, done)), = slices.seen().items()
        self.assertEqual(k, ("test", "optimizer/simplify.sql", "pairs"))
        self.assertTrue(n > 100 and done)

    def test_install_idempotent(self):
        slices.install()
        slices.install()
        from tests.helpers import load_sql_fixtures
        slices.set_filter({"identity.sql": {0}})
        self.assertEqual(len(list(load_sql_fixtures("identity.sql"))), 1)


if __name__ == "__main__":
    unittest.main()
