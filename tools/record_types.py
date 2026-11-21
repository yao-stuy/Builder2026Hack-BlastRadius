"""Record operand TYPES of every single-operator `is` / `is not` / `==` / `!=` comparison in sqlglot at the pinned
commit, and build maps/cmptypes.json.z from the union of several worker dumps.

An import hook rewrites each such ast.Compare of <root>/sqlglot/**.py at import time into
    _br_eq("path:l:c:el:ec", left, right)        (and _br_ne / _br_is / _br_isnot)
which records (site, type(left), type(right)) and then performs the ORIGINAL operator on the two operands (left
evaluated before right, each exactly once). Chained comparisons are not touched.  The site key positions come from
the pristine source.  Nothing is written to disk: files stay pristine, only the compiled code differs.

usage (inside the lab container, tree copied to /tmp/s):
  python tools/record_types.py work   --out /scratch/ct/w0.json [--slice 0/4] [--only SUBSTR] [--root /tmp/s]
  python tools/record_types.py imports --out /scratch/ct/imports.json
  python tools/record_types.py build  /scratch/ct/*.json --out maps/cmptypes.json.z
"""
import argparse
import ast
import builtins
import enum
import importlib.abc
import importlib.machinery
import json
import os
import sys
import types
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

OPS = {ast.Is: "_br_is", ast.IsNot: "_br_isnot", ast.Eq: "_br_eq", ast.NotEq: "_br_ne"}

# ---------------------------------------------------------------- recorder (lives in builtins of the worker)
SEEN = set()      # (site, id(ltype), id(rtype))
TYPES = {}        # id -> type  (keeps them alive)


def _note(s, a, b):
    ta, tb = type(a), type(b)
    k = (s, id(ta), id(tb))
    if k not in SEEN:
        SEEN.add(k)
        TYPES[id(ta)] = ta
        TYPES[id(tb)] = tb


def _br_eq(s, a, b):
    _note(s, a, b)
    return a == b


def _br_ne(s, a, b):
    _note(s, a, b)
    return a != b


def _br_is(s, a, b):
    _note(s, a, b)
    return a is b


def _br_isnot(s, a, b):
    _note(s, a, b)
    return a is not b


class _Tx(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel
        self.n = 0

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and type(node.ops[0]) in OPS:
            self.n += 1
            site = "%s:%d:%d:%d:%d" % (self.rel, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
            call = ast.Call(func=ast.Name(id=OPS[type(node.ops[0])], ctx=ast.Load()),
                            args=[ast.Constant(site), node.left, node.comparators[0]], keywords=[])
            return ast.copy_location(call, node)
        return node


class _InstLoader(importlib.machinery.SourceFileLoader):
    root = None
    count = 0

    def get_code(self, fullname):
        path = self.path
        if not path.startswith(self.root + "/sqlglot/") or not path.endswith(".py"):
            return super().get_code(fullname)
        src = self.get_data(path)
        tree = ast.parse(src, path)
        tx = _Tx(path[len(self.root) + 1:])
        tree = ast.fix_missing_locations(tx.visit(tree))
        _InstLoader.count += tx.n
        return compile(tree, path, "exec", dont_inherit=True)


def install(root):
    root = os.path.abspath(root)
    _InstLoader.root = root
    for f in (_br_eq, _br_ne, _br_is, _br_isnot):
        setattr(builtins, f.__name__, f)

    def hook(path):
        p = os.path.abspath(path)
        if os.path.isdir(p) and (p == root or p == root + "/sqlglot" or p.startswith(root + "/sqlglot/")):
            return importlib.machinery.FileFinder(path, (_InstLoader, [".py"]))
        raise ImportError

    sys.path_hooks.insert(0, hook)
    sys.path_importer_cache.clear()
    importlib.invalidate_caches()


# ---------------------------------------------------------------- type facts (computed where the types are alive)
BUILTIN_OWNERS = [object, int, float, complex, str, bytes, bytearray, tuple, list, dict, set, frozenset, range,
                  types.FunctionType, types.BuiltinFunctionType, types.MethodType, slice]
class _SamplesCls:
    def m(self):
        pass


_SAMPLES = {object: object(), int: 1, float: 1.5, complex: 1j, str: "a", bytes: b"a", bytearray: bytearray(b"a"),
            tuple: (1,), list: [1], dict: {1: 1}, set: {1}, frozenset: frozenset({1}), range: range(3),
            types.FunctionType: (lambda: 0), types.BuiltinFunctionType: len,
            types.MethodType: _SamplesCls().m, slice: slice(1)}


def _owner(T, name):
    for k in T.__mro__:
        if name in k.__dict__:
            return k
    return None


def _builtin_owner_ok(owner):
    """owner.__eq__/__ne__ of a builtin class returns NotImplemented for None (checked on a sample)."""
    if owner is None:
        return False
    if owner is object or owner in BUILTIN_OWNERS:
        return True
    return False


def _verify_builtin_samples():
    bad = []
    for o, s in _SAMPLES.items():
        for nm in ("__eq__", "__ne__"):
            f = getattr(o, nm)
            try:
                r = f(s, None)
            except Exception as e:  # noqa: BLE001
                bad.append((o, nm, repr(e)))
                continue
            if r is not NotImplemented:
                bad.append((o, nm, r))
    return bad


_EXPR_FACT = {}


def _expr_verified(root_mod):
    """sqlglot.expressions.core.Expression.__eq__/__ne__ return False/True for None (source + behaviour check)."""
    if "v" in _EXPR_FACT:
        return _EXPR_FACT["v"]
    ok = False
    try:
        import inspect
        import textwrap
        core = sys.modules["sqlglot.expressions.core"]
        E = core.Expression
        se = ast.dump(ast.parse(textwrap.dedent(inspect.getsource(E.__eq__))))
        sn = ast.dump(ast.parse(textwrap.dedent(inspect.getsource(E.__ne__))))
        want_e = ast.dump(ast.parse(
            "def __eq__(self, other: object) -> bool:\n"
            "    return self is other or (type(self) is type(other) and hash(self) == hash(other))\n"))
        want_n = ast.dump(ast.parse(
            "def __ne__(self, other: object) -> bool:\n    return not self.__eq__(other)\n"))
        from sqlglot import exp
        lit = exp.Literal.string("x")
        ok = (se == want_e and sn == want_n and (lit == None) is False and (lit != None) is True  # noqa: E711
              and (None == lit) is False and (None != lit) is True)  # noqa: E711
        _EXPR_FACT["src_eq"] = inspect.getsource(E.__eq__)
        _EXPR_FACT["src_ne"] = inspect.getsource(E.__ne__)
    except Exception as e:  # noqa: BLE001
        _EXPR_FACT["err"] = repr(e)
    _EXPR_FACT["v"] = ok
    return ok


def tname(T):
    return "%s:%s" % (T.__module__, T.__qualname__)


def type_facts(T):
    """-> dict(none_safe, enum_safe, eq, ne) for the type T (resolved by MRO at record time)."""
    eq, ne = _owner(T, "__eq__"), _owner(T, "__ne__")
    d = {"eq": tname(eq) if eq else None, "ne": tname(ne) if ne else None}
    none_safe = _builtin_owner_ok(eq) and _builtin_owner_ok(ne)
    why = "builtin/object owners" if none_safe else ""
    if not none_safe and eq is not None and ne is not None:
        if (eq.__module__ == "sqlglot.expressions.core" and eq.__qualname__ == "Expression"
                and ne.__module__ == "sqlglot.expressions.core" and ne.__qualname__ == "Expression"):
            none_safe = _expr_verified(None)
            why = "sqlglot Expression.__eq__/__ne__ verified" if none_safe else ""
    d["none_safe"] = bool(none_safe)
    d["why"] = why
    enum_safe = False
    try:
        if (isinstance(T, enum.EnumMeta) and not issubclass(T, enum.Flag) and len(T.__members__) > 0
                and _builtin_owner_ok(eq) and _builtin_owner_ok(ne)):
            ms = list(T)
            # distinct members must be distinct for the (possibly value-based) builtin __eq__ of the mixin
            enum_safe = all(m1 is m2 or not (m1 == m2) for i, m1 in enumerate(ms) for m2 in ms[i + 1:]) \
                and all(isinstance(m, T) for m in ms)
    except Exception:  # noqa: BLE001
        enum_safe = False
    d["enum_safe"] = bool(enum_safe)
    return d


def dump(path, failed=None, extra=None):
    sites = {}
    names = {}
    facts = {}
    for (s, a, b) in list(SEEN):
        ta, tb = TYPES[a], TYPES[b]
        for T in (ta, tb):
            n = tname(T)
            if n not in facts:
                facts[n] = type_facts(T)
            else:  # same qualified name for different classes (locals): keep the conservative facts
                f2 = type_facts(T)
                for k in ("none_safe", "enum_safe"):
                    facts[n][k] = facts[n][k] and f2[k]
        sites.setdefault(s, set()).add((tname(ta), tname(tb)))
    out = {"sites": {s: sorted(v) for s, v in sites.items()}, "facts": facts, "failed": failed or [],
           "instrumented": _InstLoader.count, "expr": _EXPR_FACT, "builtin_bad": [repr(x) for x in _verify_builtin_samples()]}
    if extra:
        out.update(extra)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(out, f)
    os.rename(path + ".tmp", path)


# ---------------------------------------------------------------- worker modes
def pick(tests, sl, only):
    sel = [t for t in tests if (only is None or only in t["id"])]
    if only is None:
        sel = [t for t in sel if "test_max_nodes" not in t["id"] and "test_execute_tpcds" not in t["id"]]
    if sl:
        i, n = map(int, sl.split("/"))
        groups = {}
        for t in sel:
            groups.setdefault(t["id"].split("::")[0] + "::" + t["id"].split("::")[1], []).append(t)
        load = [0.0] * n
        mine = []
        for g in sorted(groups.values(), key=lambda g: -sum(t["seconds"] for t in g)):
            k = min(range(n), key=lambda j: load[j])
            load[k] += sum(t["seconds"] for t in g)
            if k == i:
                mine.extend(g)
        mine.sort(key=lambda t: t["number"])
        sel = mine
    return sel


def ensure_root(root):
    if not os.path.isdir(os.path.join(root, "sqlglot")):
        import shutil
        shutil.copytree("/src", root, ignore=shutil.ignore_patterns(".git", "__pycache__"))


def cmd_work(a):
    ensure_root(a.root)
    install(a.root)
    sys.path.insert(0, REPO)
    from br import harness
    harness.setup(a.root)
    tests = json.load(open(a.tests))["tests"]
    sel = pick(tests, a.slice, a.only)
    ids = [t["id"] for t in sel]
    print("worker: %d tests" % len(ids), flush=True)
    res = harness.run(ids, pool_mode="inline")
    failed = [r[0] for r in res if r[1] != "pass"]
    print("worker done: %d failed %s; instrumented %d" % (len(failed), failed[:10], _InstLoader.count), flush=True)
    dump(a.out, failed, {"ran": len(ids), "cpu": sum(r[2] for r in res)})


def cmd_imports(a):
    ensure_root(a.root)
    install(a.root)
    sys.path.insert(0, a.root)
    os.chdir(a.root)
    import importlib
    import pkgutil
    import sqlglot
    bad = []
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            importlib.import_module(m.name)
        except BaseException as e:  # noqa: BLE001
            bad.append((m.name, repr(e)))
    print("import errors:", bad)
    dump(a.out, [], {"import_errors": bad})


# ---------------------------------------------------------------- map builder
def cmd_build(a):
    sites = {}
    facts = {}
    failed = []
    n_inst = 0
    expr = {}
    for p in a.inputs:
        d = json.load(open(p))
        failed += [(p, f) for f in d.get("failed", [])]
        n_inst = max(n_inst, d.get("instrumented", 0))
        if d.get("builtin_bad"):
            print("BUILTIN SAMPLE CHECK FAILED", d["builtin_bad"])
        if d.get("expr"):
            expr = d["expr"] or expr
        for k, v in d["sites"].items():
            sites.setdefault(k, set()).update(map(tuple, v))
        for n, f in d["facts"].items():
            if n in facts:
                for k in ("none_safe", "enum_safe"):
                    facts[n][k] = facts[n][k] and f[k]
            else:
                facts[n] = dict(f)
    names = sorted(facts)
    idx = {n: i for i, n in enumerate(names)}
    tab = []
    for n in names:
        f = facts[n]
        tab.append([n, (1 if f["none_safe"] else 0) | (2 if f["enum_safe"] else 0)])
    by_path = {}
    npairs = 0
    for s, prs in sorted(sites.items()):
        path, rest = s.split(":", 1)
        lst = sorted([idx[x], idx[y]] for x, y in prs)
        npairs += len(lst)
        by_path.setdefault(path, {})[rest] = lst
    out = {"v": 1, "types": tab, "sites": by_path, "instrumented_compares": n_inst,
           "expr_eq_verified": bool(expr.get("v")), "expr_eq_src": expr.get("src_eq"), "expr_ne_src": expr.get("src_ne"),
           "note": "types: [module:qualname, flags] flags bit0 = None-safe (== and != agree with is/is not against None), "
                   "bit1 = enum-safe (instances of this Enum class are singletons whose == agrees with is)"}
    raw = json.dumps(out, separators=(",", ":")).encode()
    z = zlib.compress(raw, 9)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "wb") as f:
        f.write(z)
    print("sites %d pairs %d types %d; json %d B, z %d B; failed tests in inputs: %s" %
          (len(sites), npairs, len(names), len(raw), len(z), failed))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("work")
    w.add_argument("--out", required=True)
    w.add_argument("--slice", default=None)
    w.add_argument("--only", default=None)
    w.add_argument("--root", default="/tmp/s")
    w.add_argument("--tests", default="/kit/sample/tests.json")
    i = sub.add_parser("imports")
    i.add_argument("--out", required=True)
    i.add_argument("--root", default="/tmp/s")
    b = sub.add_parser("build")
    b.add_argument("inputs", nargs="+")
    b.add_argument("--out", default=os.path.join(REPO, "maps", "cmptypes.json.z"))
    a = ap.parse_args()
    {"work": cmd_work, "imports": cmd_imports, "build": cmd_build}[a.cmd](a)


if __name__ == "__main__":
    main()
