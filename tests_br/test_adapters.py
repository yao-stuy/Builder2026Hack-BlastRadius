"""Tests for br.adapters. Need the pinned sqlglot checkout (SQLGLOT_SRC, default /src); skipped otherwise.
Run in the lab container:  python -m unittest tests_br.test_adapters
Each test class is meant to run in a fresh process only for the sqlglot import (we import once per process)."""
import os
import pickle
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("SQLGLOT_SRC", "/src")
sys.path.insert(0, REPO)

from br import adapters  # noqa: E402

HAVE = os.path.isdir(os.path.join(SRC, "sqlglot"))


@unittest.skipUnless(HAVE, "pinned sqlglot checkout not available")
class TestAdapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from br import harness
        harness.setup(SRC)

    def test_lazy_tables(self):
        import importlib
        te = importlib.import_module("tests.test_executor")
        cls = te.TestExecutor
        self.assertTrue(adapters.install_executor_lazy())
        self.assertTrue(adapters.install_executor_lazy())
        cls.setUpClass()
        try:
            t = cls.tpch_tables
            self.assertIsInstance(t, dict)
            self.assertEqual(len(t), len(te.TPCH_SCHEMA))
            self.assertIn("region", t)
            self.assertEqual(dict.__len__(t), 0)  # nothing loaded by len/contains
            r = t["region"]
            self.assertEqual(dict.__len__(t), 1)
            self.assertEqual(list(r.columns), list(te.TPCH_SCHEMA["region"]))
            t2 = pickle.loads(pickle.dumps(t))
            self.assertEqual(t2["region"].rows, r.rows)
            self.assertEqual(list(t), list(te.TPCH_SCHEMA))  # iteration loads all, original order
            self.assertEqual(dict.__len__(t), len(te.TPCH_SCHEMA))
            self.assertEqual(cls.tpch_conn.execute("select count(*) from region").fetchall(), [(5,)])
        finally:
            cls.tearDownClass()

    def test_guard_blocks_changed_source(self):
        old = adapters._hashes
        try:
            adapters._hashes = {"test_max_nodes": "0" * 40, "test_generator_memory": "0" * 40}
            self.assertIsNone(adapters.max_nodes_proxy())
            self.assertIsNone(adapters.generator_memory_run(["POSTGRES"]))
        finally:
            adapters._hashes = old

    def test_max_nodes_proxy_passes(self):
        self.assertEqual(adapters.max_nodes_proxy(), "pass")

    def test_generator_memory_subset(self):
        self.assertEqual(adapters.generator_memory_run(["postgres", "DUCKDB"]), "pass")
        self.assertIsNone(adapters.generator_memory_run(["nosuchdialect"]))

    def test_dialect_map(self):
        c = adapters._gm_load()
        self.assertTrue(c)
        base, per = c
        some = next(iter(per))
        kind, f, line = some
        self.assertIsInstance(adapters.generator_memory_dialects(f, line, kind), list)
        f0 = next(iter(base["fn"]))
        self.assertIsNone(adapters.generator_memory_dialects(f0, next(iter(base["fn"][f0])), "fn"))


if __name__ == "__main__":
    unittest.main()
