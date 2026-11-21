"""Cheap, faithful stand-ins for the expensive sqlglot tests (runtime, stdlib only).

All adapters run in the child process AFTER harness.setup(root).  Every adapter is guarded by a hash
of the pinned test source recorded at build time (maps/adapters.json, tools/record_adapters.py): if the
(patched) source differs, the adapter is not used (install -> False, proxies -> None).
Nothing here prints to stdout; every entry point is exception safe.

API
  install_executor_lazy() -> bool          lazy TPC tables/views for tests.test_executor.TestExecutor
  max_nodes_proxy() -> "pass"|"fail"|None  ~100x smaller version of TestParser.test_max_nodes
  generator_memory_run(dialects) -> "pass"|"fail"|None   TestGenerator.test_generator_memory per dialect
  generator_memory_dialects(file, line, kind) -> list[str]|None   (from maps/adapters.json.z)
  source_hash(fn) -> str
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import os
import sys
import textwrap
import unittest
import zlib

_MAPS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maps")
_hashes = None


def source_hash(fn):
    f = getattr(fn, "__func__", fn)
    return hashlib.sha1(inspect.getsource(f).encode("utf-8")).hexdigest()


def _load_hashes():
    global _hashes
    if _hashes is None:
        try:
            with open(os.path.join(_MAPS, "adapters.json"), encoding="utf-8") as fh:
                _hashes = json.load(fh).get("hashes", {})
        except Exception:
            _hashes = {}
    return _hashes


def _guard(key, fn):
    try:
        want = _load_hashes().get(key)
        return bool(want) and source_hash(fn) == want
    except Exception:
        return False


# ======================================================================================== A. executor

def _load_rows(te, directory, table, columns):
    """Verbatim row parsing of TestExecutor.setUpClass.setup()."""
    file_name = f"{directory}{table}.csv.gz"
    reader = te.csv.reader(te.open_file(file_name), delimiter="|")
    rows = []
    ctypes = []
    next(reader)
    for row in reader:
        if not ctypes:
            for i, v in enumerate(row):
                try:
                    ctypes.append(type(te.ast.literal_eval(v)))
                except (ValueError, SyntaxError):
                    ctypes.append(str)
        rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    return te.Table(columns=columns, rows=rows)


def _load_table(directory, table, columns):
    te = importlib.import_module("tests.test_executor")
    return _load_rows(te, directory, table, columns)


def _rebuild_lazy(loaded, spec):
    t = _LazyTables(spec)
    dict.update(t, loaded)
    return t


class _LazyTables(dict):
    """dict of table name -> Table; rows are parsed on first access of each table."""

    def __init__(self, spec):
        super().__init__()
        self._spec = dict(spec)  # name -> (directory, columns): not loaded yet
        self._order = list(spec)

    def _load(self, key):
        d, cols = self._spec.pop(key)
        t = _load_table(d, key, cols)
        dict.__setitem__(self, key, t)
        return t

    def _all(self):
        for k in list(self._spec):
            self._load(k)
        if len(self._order) == len(self):  # restore insertion order of the original dict
            items = [(k, dict.__getitem__(self, k)) for k in self._order if dict.__contains__(self, k)]
            dict.clear(self)
            dict.update(self, items)

    def __getitem__(self, key):
        if key in self._spec:
            return self._load(key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        if key in self._spec:
            return self._load(key)
        return dict.get(self, key, default)

    def setdefault(self, key, default=None):
        if key in self._spec:
            return self._load(key)
        return dict.setdefault(self, key, default)

    def pop(self, key, *args):
        if key in self._spec:
            self._load(key)
        return dict.pop(self, key, *args)

    def __setitem__(self, key, value):
        self._spec.pop(key, None)
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        if key in self._spec:
            self._load(key)
        dict.__delitem__(self, key)

    def __contains__(self, key):
        return key in self._spec or dict.__contains__(self, key)

    def __iter__(self):
        self._all()
        return dict.__iter__(self)

    def __len__(self):
        return len(self._spec) + dict.__len__(self)

    def __bool__(self):
        return len(self) > 0

    def __eq__(self, other):
        self._all()
        return dict.__eq__(self, other)

    __hash__ = None

    def __repr__(self):
        self._all()
        return dict.__repr__(self)

    def keys(self):
        self._all()
        return dict.keys(self)

    def values(self):
        self._all()
        return dict.values(self)

    def items(self):
        self._all()
        return dict.items(self)

    def copy(self):
        self._all()
        return dict(dict.items(self))

    def popitem(self):
        self._all()
        return dict.popitem(self)

    def update(self, *a, **k):
        for key in dict(*a, **k):
            self._spec.pop(key, None)
        dict.update(self, *a, **k)

    def clear(self):
        self._spec.clear()
        dict.clear(self)

    def __reduce__(self):
        # stays lazy across the (inline) pickle round trip; unloaded tables are re-read on demand
        return (_rebuild_lazy, (dict(dict.items(self)), dict(self._spec)))

    def __ior__(self, other):
        self._all()
        return dict.__ior__(self, other)

    def __or__(self, other):
        self._all()
        return dict.__or__(self, other)

    def __reversed__(self):
        self._all()
        return dict.__reversed__(self)


class _LazyConn:
    """duckdb connection whose per-table CSV views are created when a statement mentions the table."""

    def __init__(self, te, directory, schema):
        self._conn = te.duckdb.connect()
        self._dir = directory
        self._pending = dict(schema)  # table -> columns

    def _ensure(self, sql):
        if not self._pending:
            return
        import re
        words = set(re.findall(r"\w+", str(sql)))
        for table in [t for t in self._pending if t in words]:
            columns = self._pending.pop(table)
            file_name = f"{self._dir}{table}.csv.gz"
            self._conn.execute(
                f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
                """
            )

    def _all(self):
        for t in list(self._pending):
            self._ensure(t)

    def execute(self, query, *a, **k):
        self._ensure(query)
        return self._conn.execute(query, *a, **k)

    def sql(self, query, *a, **k):
        self._ensure(query)
        return self._conn.sql(query, *a, **k)

    def __getattr__(self, name):
        # anything else (cursor, table, close, ...): make all views exist first, then delegate
        if name.startswith("__"):
            raise AttributeError(name)
        if name != "close":
            self._all()
        return getattr(self._conn, name)


def _lazy_setup_class(cls):
    te = importlib.import_module("tests.test_executor")
    cls.tpch_conn = _LazyConn(te, te.DIR_TPCH, te.TPCH_SCHEMA)
    cls.tpcds_conn = _LazyConn(te, te.DIR_TPCDS, te.TPCDS_SCHEMA)
    cls.tpch_tables = _LazyTables({t: (te.DIR_TPCH, c) for t, c in te.TPCH_SCHEMA.items()})
    cls.tpcds_tables = _LazyTables({t: (te.DIR_TPCDS, c) for t, c in te.TPCDS_SCHEMA.items()})
    cls.cache = {}
    cls.tpch_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
    cls.tpcds_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))


def install_executor_lazy():
    """Replace TestExecutor.setUpClass by the lazy equivalent. True if installed (or already)."""
    try:
        te = importlib.import_module("tests.test_executor")
        cls = te.TestExecutor
        if getattr(cls, "_br_lazy", False):
            return True
        if not _guard("executor_setUpClass", cls.__dict__["setUpClass"].__func__):
            return False
        cls.setUpClass = classmethod(_lazy_setup_class)
        cls._br_lazy = True
        return True
    except Exception:
        return False


# ======================================================================================== B. max_nodes

_MN_TARGET = ("tests.test_parser", "TestParser", "test_max_nodes")
_MN_SCALE = {10000: 60, 1_000_000: 6_000, 50_000: 300}
_MN_MSG = ("(50000)", "(300)")


class _Scale(ast.NodeTransformer):
    def __init__(self):
        self.hits = {10000: 0, 1_000_000: 0, 50_000: 0, "msg": 0}

    def visit_Constant(self, node):
        v = node.value
        if type(v) is int and v in _MN_SCALE:
            self.hits[v] += 1
            return ast.copy_location(ast.Constant(_MN_SCALE[v]), node)
        if type(v) is str and _MN_MSG[0] in v:
            self.hits["msg"] += 1
            return ast.copy_location(ast.Constant(v.replace(_MN_MSG[0], _MN_MSG[1])), node)
        return node


_mn_cache = {}


def _mn_build(mod, cls):
    fn = cls.test_max_nodes
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    sc = _Scale()
    tree = sc.visit(tree)
    if not (sc.hits[10000] == 1 and sc.hits[1_000_000] >= 2 and sc.hits[50_000] == 1 and sc.hits["msg"] == 1):
        return None
    ast.fix_missing_locations(tree)
    code = compile(tree, "<max_nodes_proxy>", "exec")
    ns = dict(vars(mod))
    exec(code, ns)
    return ns["test_max_nodes"]


def max_nodes_proxy():
    """Run a ~100x scaled version of test_max_nodes. 'pass'/'fail', or None if not applicable."""
    try:
        mod = importlib.import_module("tests.test_parser")
        cls = mod.TestParser
        if not _guard("test_max_nodes", cls.test_max_nodes):
            return None
        fn = _mn_cache.get("fn")
        if fn is None:
            fn = _mn_cache["fn"] = _mn_build(mod, cls)
            if fn is None:
                return None
        return _run_as_method(cls, "test_max_nodes", fn)
    except Exception:
        return None


def _run_as_method(cls, name, fn):
    sub = type("_Proxy" + cls.__name__, (cls,), {name: fn})
    res = unittest.TestResult()
    # TestCase.run handles setUp/tearDown/subtests exactly like the harness does
    sub(name).run(res)
    ok = res.wasSuccessful() and not res.skipped and not res.expectedFailures
    return "pass" if ok else "fail"


# ======================================================================================== C. generator memory

_gm_cov = None


def _gm_members(dialects):
    from sqlglot.dialects import Dialects
    if dialects is None:
        return list(Dialects)
    want = {str(d).lower() for d in dialects}
    return [m for m in Dialects if m.name.lower() in want or str(m.value).lower() in want]


def generator_memory_run(dialects):
    """Run the real per-dialect body of test_generator_memory for the given dialect names (None=all)."""
    try:
        mod = importlib.import_module("tests.test_generator")
        cls = mod.TestGenerator
        if not _guard("test_generator_memory", cls.test_generator_memory):
            return None
        members = _gm_members(dialects)
        if dialects is not None and len(members) != len({str(d).lower() for d in dialects}):
            return None  # unknown name: let the caller run the real test
        src = textwrap.dedent(inspect.getsource(cls.test_generator_memory))
        ns = dict(vars(mod))
        ns["Dialects"] = members
        exec(compile(src, "<generator_memory_run>", "exec"), ns)
        return _run_as_method(cls, "test_generator_memory", ns["test_generator_memory"])
    except Exception:
        return None


def _gm_load():
    global _gm_cov
    if _gm_cov is None:
        try:
            with open(os.path.join(_MAPS, "adapters.json.z"), "rb") as fh:
                d = json.loads(zlib.decompress(fh.read()))
            gm = d["generator_memory"]
            base = {k: {f: set(_unrle(v)) for f, v in gm["base"][k].items()} for k in ("fn", "top")}
            per = {}
            for name, cov in gm["dialects"].items():
                for k in ("fn", "top"):
                    for f, v in cov[k].items():
                        for ln in _unrle(v):
                            per.setdefault((k, f, ln), []).append(name)
            _gm_cov = (base, per)
        except Exception:
            _gm_cov = False
    return _gm_cov


def _unrle(v):
    """delta encoding: [first, d1, d2, ...]"""
    out = []
    cur = 0
    for i, x in enumerate(v):
        cur = x if i == 0 else cur + x
        out.append(cur)
    return out


def generator_memory_dialects(file, line, kind):
    """Dialects whose own run of the test covers (file, line). None = line is in the base coverage
    (all dialects, or unknown). kind: 'fn' (function bodies) or 'top' (module/class level)."""
    try:
        c = _gm_load()
        if not c:
            return None
        base, per = c
        if line in base.get(kind, {}).get(file, ()):
            return None
        return list(per.get((kind, file, line), []))
    except Exception:
        return None
