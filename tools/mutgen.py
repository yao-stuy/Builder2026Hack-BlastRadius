#!/usr/bin/env python3
"""mutgen: generator of synthetic patches ("mutants") against a pinned sqlglot checkout.

Benchmark tooling only. It is never used at run time and its output is never shipped.

    python tools/mutgen.py --out /scratch/bench/<set> --seed S --n N --profile sample|novel|mixed
    python tools/mutgen.py --list-kinds
    python tools/mutgen.py --out ... --kind cmp_flip --n 20

Output: <out>/commits/<id>.patch and <out>/meta.json (id -> {kind, subkind, files, description}).
Patches are produced by copying the checkout to a temp dir, git init + commit once, applying
the edit to the working tree and taking `git diff --cached`, so the format is exactly git's.
Deterministic for a given (seed, n, profile, kind, source tree).
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import tempfile
import tokenize
from collections import Counter, defaultdict

DEFAULT_SRC = "/src"

# --------------------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------------------


def apply_edits(lines, edits):
    """edits: (lineno(1-based), byte_start, byte_end, replacement) on single lines."""
    new = list(lines)
    for ln, b0, b1, repl in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        bl = new[ln - 1].encode("utf-8")
        new[ln - 1] = (bl[:b0] + repl.encode("utf-8") + bl[b1:]).decode("utf-8")
    return new


def indent_of(line):
    return line[: len(line) - len(line.lstrip(" \t"))]


def describe(rel, a, b, what=""):
    i = 0
    m = min(len(a), len(b))
    while i < m and a[i] == b[i]:
        i += 1
    pre = f"{what}: " if what else ""
    if len(b) > len(a):
        n = len(b) - len(a)
        return f"{pre}{rel}:{i + 1}: add {n} line(s) starting {b[i].strip()[:90]!r}"
    if len(b) < len(a):
        n = len(a) - len(b)
        return f"{pre}{rel}:{i + 1}: delete {n} line(s) starting {a[i].strip()[:90]!r}"
    return f"{pre}{rel}:{i + 1}: {a[i].strip()[:90]!r} -> {b[i].strip()[:90]!r}"


def tweak_str(seg, rng):
    """Change one alphanumeric character inside a simple one-line string literal."""
    if len(seg) < 3 or seg[0] not in "\"'" or seg[-1] != seg[0] or seg[:3] in ('"""', "'''"):
        return None
    inner = seg[1:-1]
    if "\\" in inner or not inner:
        return None
    pos = [i for i, c in enumerate(inner) if c.isalnum() and c.isascii()]
    if not pos:
        return None
    i = rng.choice(pos)
    c = inner[i]
    if c.isdigit():
        nc = str((int(c) + rng.choice([1, 9])) % 10)
    elif rng.random() < 0.5:
        nc = c.swapcase()
    else:
        nc = chr((ord(c.lower()) - 97 + 1) % 26 + 97)
        if c.isupper():
            nc = nc.upper()
    return seg[0] + inner[:i] + nc + inner[i + 1 :] + seg[0]


INS_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789 (),.<>=*"


def char_edit(line, rng, mode=None, alnum_only=False):
    """One-character insertion / deletion / replacement in a line. Returns new line or None."""
    if not line.strip():
        return None
    mode = mode or rng.choice(["ins", "del", "repl"])
    n = len(line)
    if mode == "ins":
        p = rng.randrange(0, n + 1)
        pool = string.ascii_lowercase + string.digits if alnum_only else INS_CHARS
        c = rng.choice(pool)
        if not alnum_only and rng.random() < 0.3:
            c = c + " "
        return line[:p] + c + line[p:]
    idxs = [i for i, ch in enumerate(line) if not ch.isspace()]
    if alnum_only:
        idxs = [i for i in idxs if line[i].isalnum() and line[i].isascii()]
    if not idxs:
        return None
    p = rng.choice(idxs)
    if mode == "del":
        return line[:p] + line[p + 1 :]
    ch = line[p]
    if ch.isdigit():
        nc = rng.choice([d for d in string.digits if d != ch])
    elif ch.isalpha():
        pool = string.ascii_uppercase if ch.isupper() else string.ascii_lowercase
        nc = rng.choice([d for d in pool if d != ch])
    else:
        nc = rng.choice([d for d in "(),.<>=*+-_ " if d != ch])
    return line[:p] + nc + line[p + 1 :]


COMMENT_TEXTS = [
    "Cheap check first",
    "Keep in sync with the generator",
    "Intentionally not cached",
    "Mirrors the behavior of the reference implementation",
    "Order matters here",
    "Fast path for the common case",
    "See the dialect docs for details",
    "This must run before normalization",
    "Handled separately below",
    "Preserve the original behavior",
    "Avoid mutating the input expression",
    "Needed for backwards compatibility",
    "Only applies to the top-level node",
    "TODO: revisit once the parser is refactored",
    "Edge case: empty input",
    "Copy before modifying",
    "Not all dialects support this",
    "Invariant: arguments are already validated",
    "Falls through to the default handling",
    "Shared with the base implementation",
]

DIALECTS = [
    "bigquery", "snowflake", "duckdb", "postgres", "mysql", "spark", "tsql", "oracle", "hive",
    "presto", "trino", "clickhouse", "redshift", "sqlite", "databricks", "starrocks", "doris",
]


def string_rows(text):
    """Return list of (start_row, end_row) of multi-line string tokens (1-based)."""
    spans, stack = [], []
    fs, fe = getattr(tokenize, "FSTRING_START", None), getattr(tokenize, "FSTRING_END", None)
    try:
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            if t.type == tokenize.STRING and t.start[0] < t.end[0]:
                spans.append((t.start[0], t.end[0]))
            elif fs is not None and t.type == fs:
                stack.append(t.start[0])
            elif fe is not None and t.type == fe:
                s = stack.pop() if stack else t.end[0]
                if s < t.end[0]:
                    spans.append((s, t.end[0]))
    except (tokenize.TokenError, SyntaxError):
        pass
    return spans


class FileInfo:
    def __init__(self, rel, text):
        self.rel = rel
        self.text = text
        self.lines = text.split("\n")
        self.bl = [l.encode("utf-8") for l in self.lines]
        self.tree = None
        if rel.endswith(".py"):
            try:
                self.tree = ast.parse(text)
            except SyntaxError:
                self.tree = None
        self._par = None
        self._cands = None
        self._rows = None

    def parents(self):
        if self._par is None:
            self._par = {}
            for n in ast.walk(self.tree):
                for c in ast.iter_child_nodes(n):
                    self._par[c] = n
        return self._par

    def rows(self):
        """(all_string_rows, inner_string_rows)"""
        if self._rows is None:
            allr, inner = set(), set()
            for s, e in string_rows(self.text):
                allr.update(range(s, e + 1))
                inner.update(range(s + 1, e + 1))
            self._rows = (allr, inner)
        return self._rows

    def bodies(self):
        for n in ast.walk(self.tree):
            for fld in ("body", "orelse", "finalbody"):
                b = getattr(n, fld, None)
                if isinstance(b, list) and b and isinstance(b[0], ast.stmt):
                    yield n, b

    def starts_line(self, n):
        return self.bl[n.lineno - 1][: n.col_offset].strip() == b""

    def ends_line(self, n, allow_comma=False):
        rest = self.bl[n.end_lineno - 1][n.end_col_offset :].strip()
        if allow_comma and rest.startswith(b","):
            rest = rest[1:].strip()
        return rest == b"" or rest.startswith(b"#")

    def cands(self):
        if self._cands is None:
            self._cands = collect_code(self)
        return self._cands


# --------------------------------------------------------------------------------------
# AST candidate collection for code mutations
# --------------------------------------------------------------------------------------

OPRE = re.compile(r"\b(is\s+not|not\s+in|is|in)\b|==|!=|<=|>=|<|>")
CMP_MAP = {
    "==": ("cmp_flip", ["!="]),
    "!=": ("cmp_flip", ["=="]),
    "in": ("cmp_flip", ["not in"]),
    "not in": ("cmp_flip", ["in"]),
    "<": ("cmp_bound", ["<=", ">"]),
    "<=": ("cmp_bound", ["<"]),
    ">": ("cmp_bound", [">=", "<"]),
    ">=": ("cmp_bound", [">"]),
    "is": ("is_eq", ["==", "is not"]),
    "is not": ("is_eq", ["!=", "is"]),
}


def collect_code(fi):
    out = defaultdict(list)
    if fi.tree is None:
        return out
    bl = fi.bl
    par = fi.parents()

    def seg(n):
        return bl[n.lineno - 1][n.col_offset : n.end_col_offset].decode("utf-8")

    def one(n):
        return n.lineno == n.end_lineno

    def add(sub, ln, fn):
        out[sub].append((ln, fn))

    def fixed(ln, b0, b1, opts):
        return lambda rng: [(ln, b0, b1, rng.choice(opts))]

    def gap_chars(a_ln, a_c, b_ln, b_c):
        res = []
        if a_ln == b_ln:
            for k, ch in enumerate(bl[a_ln - 1][a_c:b_c]):
                res.append((ch, a_ln, a_c + k))
        else:
            for k, ch in enumerate(bl[a_ln - 1][a_c:]):
                res.append((ch, a_ln, a_c + k))
            for ln in range(a_ln + 1, b_ln):
                for k, ch in enumerate(bl[ln - 1]):
                    res.append((ch, ln, k))
                res.append((10, ln, len(bl[ln - 1])))
            for k, ch in enumerate(bl[b_ln - 1][:b_c]):
                res.append((ch, b_ln, k))
        return res

    def classify(n):
        q, p = n, par.get(n)
        while isinstance(p, ast.UnaryOp) and isinstance(p.op, (ast.USub, ast.UAdd)):
            q, p = p, par.get(p)
        if isinstance(p, ast.arguments):
            return "default"
        if isinstance(p, ast.keyword):
            return "kw"
        if isinstance(p, ast.Subscript) and p.slice is q:
            return "index"
        if isinstance(p, ast.Call) and q in p.args:
            f = p.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            return "index" if name == "seq_get" else "callarg"
        if isinstance(p, ast.Dict) and q in p.values:
            return "dictval"
        return "plain"

    for n in ast.walk(fi.tree):
        if isinstance(n, ast.Compare):
            for i, op in enumerate(n.ops):
                left = n.left if i == 0 else n.comparators[i - 1]
                comp = n.comparators[i]
                if left.end_lineno != comp.lineno:
                    continue
                ln = comp.lineno
                gap = bl[ln - 1][left.end_col_offset : comp.col_offset].decode("utf-8", "replace")
                m = OPRE.search(gap)
                if not m:
                    continue
                txt = re.sub(r"\s+", " ", m.group())
                if txt not in CMP_MAP:
                    continue
                sub, opts = CMP_MAP[txt]
                add(sub, ln, fixed(ln, left.end_col_offset + m.start(), left.end_col_offset + m.end(), opts))
        elif isinstance(n, ast.BoolOp):
            kw = "and" if isinstance(n.op, ast.And) else "or"
            other = "or" if kw == "and" else "and"
            for a, b in zip(n.values, n.values[1:]):
                g = gap_chars(a.end_lineno, a.end_col_offset, b.lineno, b.col_offset)
                raw = bytes(c[0] for c in g)
                if b"#" in raw:
                    continue
                try:
                    s = raw.decode("ascii")
                except UnicodeDecodeError:
                    continue
                m = re.search(r"\b(and|or)\b", s)
                if not m or m.group() != kw:
                    continue
                _, ln, b0 = g[m.start()]
                add("boolop", ln, fixed(ln, b0, b0 + len(kw), [other]))
        elif isinstance(n, (ast.If, ast.While, ast.IfExp)):
            t = n.test
            if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not):
                o = t.operand
                if t.lineno == o.lineno and re.fullmatch(rb"not\s+", bl[t.lineno - 1][t.col_offset : o.col_offset]):
                    add("not_toggle", t.lineno, fixed(t.lineno, t.col_offset, o.col_offset, [""]))
            elif isinstance(t, ast.BoolOp):
                if one(t):
                    ln = t.lineno
                    add("not_toggle", ln, lambda rng, t=t, ln=ln: [
                        (ln, t.col_offset, t.col_offset, "not ("), (ln, t.end_col_offset, t.end_col_offset, ")")])
            elif isinstance(t, (ast.Name, ast.Attribute, ast.Call, ast.Subscript, ast.Compare)):
                add("not_toggle", t.lineno, fixed(t.lineno, t.col_offset, t.col_offset, ["not "]))
        elif isinstance(n, ast.Constant) and one(n):
            v = n.value
            s = seg(n)
            k = classify(n)
            ln = n.lineno
            c0, c1 = n.col_offset, n.end_col_offset
            if isinstance(v, bool):
                sub = {"kw": "kwarg_value", "default": "default_arg"}.get(k, "bool_flip")
                add(sub, ln, fixed(ln, c0, c1, ["False" if v else "True"]))
            elif isinstance(v, int) and re.fullmatch(r"\d+", s):
                sub = {"index": "index_arg", "callarg": "int_arg", "kw": "kwarg_value",
                       "default": "default_arg"}.get(k, "const_num")
                opts = [str(v + 1)] + ([str(v - 1)] if v > 0 else [])
                add(sub, ln, fixed(ln, c0, c1, opts))
            elif isinstance(v, str) and k in ("dictval", "kw", "default"):
                sub = {"dictval": "dict_str", "kw": "kwarg_value", "default": "default_arg"}[k]

                def mk(rng, ln=ln, c0=c0, c1=c1, s=s):
                    r = tweak_str(s, rng)
                    return [(ln, c0, c1, r)] if r and r != s else None
                if tweak_str(s, random.Random(0)):
                    add(sub, ln, mk)
    return out


SAMPLE_CODE = {
    "cmp_flip": 2.0, "cmp_bound": 1.6, "boolop": 2.0, "not_toggle": 2.0, "const_num": 2.0,
    "bool_flip": 1.5, "is_eq": 1.0, "index_arg": 1.0, "int_arg": 1.0, "kwarg_value": 1.5,
    "dict_str": 1.5,
}
CODE_DOC = {
    "cmp_flip": "== / != / in / not in flip",
    "cmp_bound": "< <= > >= change",
    "boolop": "and <-> or",
    "not_toggle": "insert/remove `not` on an if/while test",
    "const_num": "numeric constant +-1",
    "bool_flip": "True <-> False",
    "is_eq": "is / is not <-> == / != (or is <-> is not)",
    "index_arg": "subscript index / seq_get index change",
    "int_arg": "integer positional call argument change",
    "kwarg_value": "keyword-argument value change",
    "dict_str": "string literal tweak in a dict value",
}

# --------------------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------------------


class Ctx:
    def __init__(self, src):
        self.src = src
        self._text = {}
        self._info = {}
        self.rng = random.Random(0)
        self.avoid = set()
        self.files = []
        for root, dirs, fs in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__", ".mypy_cache"))
            for f in sorted(fs):
                p = os.path.join(root, f)
                self.files.append(os.path.relpath(p, src).replace(os.sep, "/"))
        self.fileset = set(self.files)
        self.size = {}
        self.sg_py = [f for f in self.files if f.startswith("sqlglot/") and f.endswith(".py")]
        self.tests_py = [f for f in self.files if f.startswith("tests/") and f.endswith(".py")
                         and "/fixtures/" not in f and f != "tests/__init__.py"]
        self.opt_sql = [f for f in self.files if re.fullmatch(r"tests/fixtures/optimizer/[^/]+\.sql", f)]
        self.sql_all = [f for f in self.files if f.startswith("tests/fixtures/") and f.endswith(".sql")]

    def text(self, rel):
        if rel not in self._text:
            with open(os.path.join(self.src, rel), encoding="utf-8", newline="") as f:
                self._text[rel] = f.read()
        return self._text[rel]

    def info(self, rel):
        if rel not in self._info:
            self._info[rel] = FileInfo(rel, self.text(rel))
        return self._info[rel]

    def fsize(self, rel):
        if rel not in self.size:
            self.size[rel] = os.path.getsize(os.path.join(self.src, rel))
        return self.size[rel]

    def pick(self, pool, weighted=True):
        pool = [f for f in pool if f not in self.avoid and f in self.fileset]
        if not pool:
            return None
        if weighted:
            return self.rng.choices(pool, [self.fsize(f) + 1 for f in pool])[0]
        return self.rng.choice(pool)


def compiles(rel, text):
    try:
        compile(text, rel, "exec")
        return True
    except (SyntaxError, ValueError, RecursionError):
        return False


def mk_edit(ctx, rel, fi, newlines, what="", check=True):
    """Build an Edit dict from a modified line list; None if unchanged / does not compile."""
    new = "\n".join(newlines)
    if new == fi.text:
        return None
    if check and rel.endswith(".py") and not compiles(rel, new):
        return None
    return {"files": {rel: new}, "desc": describe(rel, fi.lines, newlines, what)}


# --------------------------------------------------------------------------------------
# generators. Each returns an Edit {"files": {rel: text|None}, "desc": str} or None.
# --------------------------------------------------------------------------------------


def code_edit_in(ctx, rel, subs, tries=20):
    fi = ctx.info(rel)
    if fi.tree is None:
        return None
    pool = [(s, c) for s in subs for c in fi.cands().get(s, [])]
    for _ in range(tries):
        if not pool:
            return None
        sub, (ln, make) = ctx.rng.choice(pool)
        edits = make(ctx.rng)
        if not edits:
            continue
        e = mk_edit(ctx, rel, fi, apply_edits(fi.lines, edits), sub)
        if e:
            return e
    return None


def gen_code(sub):
    def g(ctx):
        for _ in range(60):
            rel = ctx.pick(ctx.sg_py)
            if rel is None:
                return None
            if not ctx.info(rel).cands().get(sub):
                continue
            e = code_edit_in(ctx, rel, [sub], tries=5)
            if e:
                return e
        return None
    return g


def gen_fixture(mode):
    def g(ctx):
        for _ in range(40):
            rel = ctx.pick(ctx.opt_sql)
            if rel is None:
                return None
            fi = ctx.info(rel)
            idx = [i for i, l in enumerate(fi.lines) if l.strip() and not l.startswith("#") and not l.startswith("--")]
            if not idx:
                continue
            i = ctx.rng.choice(idx)
            nl = char_edit(fi.lines[i], ctx.rng, mode)
            if nl is None or nl == fi.lines[i]:
                continue
            new = list(fi.lines)
            new[i] = nl
            e = mk_edit(ctx, rel, fi, new, "fixture")
            if e:
                return e
        return None
    return g


def pick_comment_pos(fi, rng, tries=50):
    _, inner = fi.rows()
    n = len(fi.lines) - 1  # last element is '' after trailing newline
    for _ in range(tries):
        i = rng.randrange(0, max(1, n))
        if (i + 1) in inner:
            continue
        if i > 0 and fi.lines[i - 1].rstrip().endswith("\\"):
            continue
        if not fi.lines[i].strip():
            continue
        return i
    return None


def gen_comment(ctx):
    for _ in range(30):
        rel = ctx.pick(ctx.sg_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        i = pick_comment_pos(fi, ctx.rng)
        if i is None:
            continue
        txt = ctx.rng.choice(COMMENT_TEXTS)
        new = list(fi.lines)
        new.insert(i, indent_of(fi.lines[i]) + "# " + txt)
        e = mk_edit(ctx, rel, fi, new, "comment")
        if e:
            return e
    return None


def merge(edits, sep="; "):
    files, descs = {}, []
    for e in edits:
        for k, v in e["files"].items():
            if k in files:
                return None
            files[k] = v
        descs.append(e["desc"])
    return {"files": files, "desc": sep.join(descs)}


def gen_multi_from(pool, lo=2, hi=3):
    """pool: list of (name, generator, weight)."""
    def g(ctx):
        k = 2 if ctx.rng.random() < 0.7 or hi == 2 else 3
        parts = []
        saved = set(ctx.avoid)
        try:
            for _ in range(k):
                ok = None
                for _t in range(6):
                    name, fn, _w = ctx.rng.choices(pool, [p[2] for p in pool])[0]
                    e = fn(ctx)
                    if e:
                        ok = e
                        break
                if not ok:
                    return None
                parts.append(ok)
                ctx.avoid |= set(ok["files"])
        finally:
            ctx.avoid = saved
        return merge(parts)
    return g


# ---- novel generators -----------------------------------------------------------------


def gen_test_expected(ctx):
    for _ in range(40):
        rel = ctx.pick(ctx.tests_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None:
            continue
        par = fi.parents()
        pref, other = [], []
        for n in ast.walk(fi.tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.lineno == n.end_lineno and len(n.value) >= 5:
                p = par.get(n)
                q = p
                while isinstance(q, (ast.keyword, ast.Tuple, ast.List)):
                    q = par.get(q)
                if isinstance(q, ast.Call):
                    f = q.func
                    name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                    (pref if name.startswith(("validate", "assert")) else other).append(n)
        pool = pref if pref and (ctx.rng.random() < 0.9 or not other) else other
        if not pool:
            continue
        n = ctx.rng.choice(pool)
        s = fi.bl[n.lineno - 1][n.col_offset : n.end_col_offset].decode("utf-8")
        r = tweak_str(s, ctx.rng)
        if not r or r == s:
            continue
        e = mk_edit(ctx, rel, fi, apply_edits(fi.lines, [(n.lineno, n.col_offset, n.end_col_offset, r)]), "test-expected")
        if e:
            return e
    return None


ALL_SUBS = list(SAMPLE_CODE)


def gen_test_helper(ctx):
    for _ in range(10):
        rel = ctx.rng.choice(["tests/helpers.py", "tests/dialects/test_dialect.py"])
        if rel in ctx.avoid or rel not in ctx.fileset:
            continue
        e = code_edit_in(ctx, rel, ALL_SUBS)
        if e:
            e["desc"] = "test-helper " + e["desc"]
            return e
    return None


OTHER_FX = ["tests/fixtures/identity.sql", "tests/fixtures/pretty.sql", "tests/fixtures/partial.sql",
            "tests/fixtures/jsonpath/cts.json", "tests/fixtures/optimizer/tpc-h/tpc-h.sql",
            "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"]


def gen_fixture_other(ctx):
    for _ in range(40):
        rel = ctx.pick(OTHER_FX)
        if rel is None:
            return None
        fi = ctx.info(rel)
        is_json = rel.endswith(".json")
        if is_json:
            idx = [i for i, l in enumerate(fi.lines) if '"' in l and l.strip()]
        else:
            idx = [i for i, l in enumerate(fi.lines) if l.strip() and not l.startswith(("#", "--"))]
        if not idx:
            continue
        i = ctx.rng.choice(idx)
        nl = char_edit(fi.lines[i], ctx.rng, None, alnum_only=is_json)
        if nl is None or nl == fi.lines[i]:
            continue
        new = list(fi.lines)
        new[i] = nl
        e = mk_edit(ctx, rel, fi, new, "fixture-other")
        if e:
            return e
    return None


META_RE = re.compile(r"^(#|--)\s?(dialect|title|execute|schema|note|execute_min_python|canonicalize_table_aliases):?")


def gen_fixture_meta(ctx):
    for _ in range(40):
        rel = ctx.pick(ctx.sql_all)
        if rel is None:
            return None
        fi = ctx.info(rel)
        idx = [i for i, l in enumerate(fi.lines) if l.startswith(("# ", "-- ", "#dialect"))]
        if not idx:
            continue
        i = ctx.rng.choice(idx)
        line = fi.lines[i]
        new = list(fi.lines)
        r = ctx.rng.random()
        m = re.match(r"^# dialect: ?(.*)$", line)
        if m and r < 0.6:
            cur = m.group(1).split(",")[0].strip()
            nd = ctx.rng.choice([d for d in DIALECTS if d != cur])
            new[i] = "# dialect: " + nd
        elif line.startswith("# execute: ") and r < 0.7:
            new[i] = "# execute: false" if "true" in line else "# execute: true"
        elif r < 0.75:
            nl = char_edit(line[3:] if line.startswith("-- ") or line.startswith("# ") else line, ctx.rng, "repl", alnum_only=True)
            if nl is None:
                continue
            new[i] = line[:3] + nl
        elif r < 0.9:
            del new[i]
        else:
            j = ctx.rng.choice([k for k, l in enumerate(fi.lines) if l.strip() and not l.startswith(("#", "--"))] or [0])
            new.insert(j, "# dialect: " + ctx.rng.choice(DIALECTS))
        e = mk_edit(ctx, rel, fi, new, "fixture-meta")
        if e:
            return e
    return None


SIMPLE_STMT = (ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Return, ast.Raise, ast.Assert, ast.Delete)


def one_line_stmt(fi, st):
    return (isinstance(st, SIMPLE_STMT) and st.lineno == st.end_lineno and fi.starts_line(st) and fi.ends_line(st)
            and not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)))


def gen_stmt_delete(ctx):
    for _ in range(40):
        rel = ctx.pick(ctx.sg_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None:
            continue
        pool = [(st, b) for _n, b in fi.bodies() for st in b if one_line_stmt(fi, st)]
        if not pool:
            continue
        st, b = ctx.rng.choice(pool)
        new = list(fi.lines)
        if len(b) == 1:
            new[st.lineno - 1] = indent_of(new[st.lineno - 1]) + "pass"
        else:
            del new[st.lineno - 1]
        e = mk_edit(ctx, rel, fi, new, "stmt-delete")
        if e:
            return e
    return None


def gen_func_delete(ctx):
    for _ in range(40):
        rel = ctx.pick(ctx.sg_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None:
            continue
        pool = []
        for _n, b in fi.bodies():
            for st in b:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    s = min([d.lineno for d in st.decorator_list] + [st.lineno])
                    if st.end_lineno - s + 1 <= 9 and fi.starts_line(st):
                        pool.append((st, b, s))
        if not pool:
            continue
        st, b, s = ctx.rng.choice(pool)
        new = list(fi.lines)
        if len(b) == 1:
            new[s - 1 : st.end_lineno] = [indent_of(fi.lines[st.lineno - 1]) + "pass"]
        else:
            del new[s - 1 : st.end_lineno]
            k = s - 1
            if 0 < k < len(new) and not new[k - 1].strip() and not new[k].strip():
                del new[k]
        e = mk_edit(ctx, rel, fi, new, "func-delete")
        if e:
            return e
    return None


def entry_spans(fi):
    """Deletable whole-line entries of multi-line dict/list/set literals: (first_line, last_line)."""
    out = []
    for n in ast.walk(fi.tree):
        items = []
        if isinstance(n, ast.Dict):
            items = [(k, v) for k, v in zip(n.keys, n.values) if k is not None]
            if len(items) != len(n.keys):
                continue
        elif isinstance(n, (ast.List, ast.Set)):
            items = [(e, e) for e in n.elts]
        if len(items) < 2 or n.lineno == n.end_lineno:
            continue
        prev_end = n.lineno
        for a, z in items:
            ok = fi.starts_line(a) and fi.ends_line(z, allow_comma=True) and a.lineno > prev_end
            prev_end = z.end_lineno
            if ok and z.end_lineno - a.lineno < 4:
                out.append((a.lineno, z.end_lineno))
    return out


def gen_entry_delete(ctx):
    for _ in range(40):
        rel = ctx.pick(ctx.sg_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None:
            continue
        sp = entry_spans(fi)
        if not sp:
            continue
        a, z = ctx.rng.choice(sp)
        new = list(fi.lines)
        del new[a - 1 : z]
        e = mk_edit(ctx, rel, fi, new, "entry-delete")
        if e:
            return e
    return None


def docstring_ranges(fi):
    out = []
    for n in ast.walk(fi.tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                out.append((b[0].lineno, b[0].end_lineno))
    return out


def gen_doc_word(ctx):
    for _ in range(60):
        rel = ctx.pick(ctx.sg_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None:
            continue
        rs = docstring_ranges(fi)
        if not rs:
            continue
        a, z = ctx.rng.choice(rs)
        idx = [i for i in range(a - 1, z) if ">>>" not in fi.lines[i] and "\\" not in fi.lines[i]
               and re.search(r"[A-Za-z]{4,}", fi.lines[i])]
        if not idx:
            continue
        i = ctx.rng.choice(idx)
        line = fi.lines[i]
        ms = list(re.finditer(r"[A-Za-z]{4,}", line))
        m = ctx.rng.choice(ms)
        w = m.group()
        nw = w[:-1] if w.endswith("s") else w + "s"
        new = list(fi.lines)
        new[i] = line[: m.start()] + nw + line[m.end():]
        e = mk_edit(ctx, rel, fi, new, "docstring")
        if e:
            return e
    return None


def gen_doc_doctest(ctx):
    for _ in range(80):
        rel = ctx.pick(ctx.sg_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None or ">>>" not in fi.text:
            continue
        pool = []
        for a, z in docstring_ranges(fi):
            i = a - 1
            while i < z:
                if fi.lines[i].strip().startswith(">>>"):
                    j = i + 1
                    while j < z and fi.lines[j].strip().startswith("..."):
                        j += 1
                    while j < z and fi.lines[j].strip() and not fi.lines[j].strip().startswith((">>>", '"""')):
                        if "\\" not in fi.lines[j] and re.search(r"[A-Za-z0-9]", fi.lines[j]):
                            pool.append(j)
                        j += 1
                    i = j
                else:
                    i += 1
        if not pool:
            continue
        i = ctx.rng.choice(pool)
        line = fi.lines[i]
        ind = indent_of(line)
        nl = char_edit(line[len(ind):], ctx.rng, "repl", alnum_only=True)
        if nl is None:
            continue
        new = list(fi.lines)
        new[i] = ind + nl
        e = mk_edit(ctx, rel, fi, new, "doctest")
        if e:
            return e
    return None


def gen_syntax(ctx):
    for _ in range(60):
        rel = ctx.pick(ctx.sg_py + ctx.tests_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None:
            continue
        variant = ctx.rng.choice(["paren", "extra_paren", "colon", "indent", "dedent", "quote"])
        new = None
        bl = fi.bl
        if variant == "paren":
            pool = [n for n in ast.walk(fi.tree) if isinstance(n, ast.Call) and n.lineno == n.end_lineno
                    and bl[n.end_lineno - 1][n.end_col_offset - 1 : n.end_col_offset] == b")"]
            if pool:
                n = ctx.rng.choice(pool)
                new = apply_edits(fi.lines, [(n.lineno, n.end_col_offset - 1, n.end_col_offset, "")])
        elif variant == "extra_paren":
            pool = [st for _n, b in fi.bodies() for st in b if one_line_stmt(fi, st)]
            if pool:
                st = ctx.rng.choice(pool)
                new = apply_edits(fi.lines, [(st.lineno, st.end_col_offset, st.end_col_offset, ")")])
        elif variant == "colon":
            pool = [n for n in ast.walk(fi.tree)
                    if isinstance(n, (ast.If, ast.For, ast.While, ast.With, ast.FunctionDef, ast.ClassDef))
                    and n.body[0].lineno == n.lineno + 1 and fi.lines[n.lineno - 1].rstrip().endswith(":")]
            if pool:
                n = ctx.rng.choice(pool)
                i = n.lineno - 1
                new = list(fi.lines)
                new[i] = fi.lines[i].rstrip()[:-1]
        elif variant in ("indent", "dedent"):
            pool = [st for _n, b in fi.bodies() for k, st in enumerate(b) if k > 0 and fi.starts_line(st)
                    and st.lineno not in fi.rows()[1]]
            if pool:
                st = ctx.rng.choice(pool)
                i = st.lineno - 1
                new = list(fi.lines)
                if variant == "indent":
                    new[i] = " " + new[i]
                elif len(indent_of(new[i])) >= 2:
                    new[i] = new[i][2:]
        elif variant == "quote":
            pool = [n for n in ast.walk(fi.tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.lineno == n.end_lineno and bl[n.lineno - 1][n.col_offset : n.col_offset + 1] in (b'"', b"'")
                    and bl[n.lineno - 1][n.col_offset : n.col_offset + 3] not in (b'"""', b"'''")
                    and bl[n.end_lineno - 1][n.end_col_offset - 1 : n.end_col_offset] in (b'"', b"'")]
            if pool:
                n = ctx.rng.choice(pool)
                new = apply_edits(fi.lines, [(n.lineno, n.end_col_offset - 1, n.end_col_offset, "")])
        if not new or "\n".join(new) == fi.text:
            continue
        if compiles(rel, "\n".join(new)):
            continue
        e = mk_edit(ctx, rel, fi, new, "syntax-" + variant, check=False)
        if e:
            return e
    return None


def gen_ws(variant):
    def g(ctx):
        for _ in range(60):
            rel = ctx.pick(ctx.sg_py)
            if rel is None:
                return None
            fi = ctx.info(rel)
            if fi.tree is None:
                continue
            allr, inner = fi.rows()
            n = len(fi.lines) - 1
            new = list(fi.lines)
            if variant == "trailing":
                idx = [i for i in range(n) if fi.lines[i].strip() and (i + 1) not in allr]
                if not idx:
                    continue
                i = ctx.rng.choice(idx)
                new[i] = new[i] + " " * ctx.rng.choice([1, 1, 2, 4])
            elif variant == "blank_insert":
                idx = [i for i in range(1, n) if (i + 1) not in inner and fi.lines[i].strip()
                       and not fi.lines[i - 1].rstrip().endswith("\\")]
                if not idx:
                    continue
                i = ctx.rng.choice(idx)
                new.insert(i, "")
            elif variant == "blank_delete":
                idx = [i for i in range(1, n) if not fi.lines[i].strip() and (i + 1) not in inner]
                if not idx:
                    continue
                i = ctx.rng.choice(idx)
                del new[i]
            else:  # reindent a continuation line inside brackets
                pool = []
                for nd in ast.walk(fi.tree):
                    if isinstance(nd, (ast.Call, ast.Dict, ast.List)) and nd.end_lineno - nd.lineno >= 2:
                        pool.extend(range(nd.lineno + 1, nd.end_lineno))
                pool = [ln for ln in pool if ln not in allr and fi.lines[ln - 1].strip()]
                if not pool:
                    continue
                i = ctx.rng.choice(pool) - 1
                new[i] = " " * ctx.rng.choice([1, 2, 4]) + new[i]
            e = mk_edit(ctx, rel, fi, new, "whitespace-" + variant)
            if e:
                return e
        return None
    return g


UNUSED_FUNCS = [
    ["def _unused_helper(value):", "    return value"],
    ["def _noop(*args, **kwargs):", "    return None"],
    ["def _debug_repr(obj) -> str:", "    return repr(obj)"],
    ["def _identity(x):", '    """Return the argument unchanged."""', "    return x"],
]


def gen_add_func(ctx):
    for _ in range(40):
        rel = ctx.pick(ctx.sg_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None or not fi.tree.body:
            continue
        st = ctx.rng.choice(fi.tree.body)
        body = ctx.rng.choice(UNUSED_FUNCS)
        name = "_unused_%s" % "".join(ctx.rng.choices(string.ascii_lowercase, k=3))
        body = [body[0].replace(body[0].split("(")[0].split()[1], name)] + body[1:]
        new = list(fi.lines)
        new[st.end_lineno : st.end_lineno] = ["", ""] + body
        e = mk_edit(ctx, rel, fi, new, "add-function")
        if e:
            return e
    return None


def gen_add_import(ctx):
    for _ in range(40):
        rel = ctx.pick(ctx.sg_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None:
            continue
        last = None
        for st in fi.tree.body:
            if isinstance(st, (ast.Import, ast.ImportFrom)):
                last = st
            elif last is not None:
                break
        if last is None:
            continue
        mod = ctx.rng.choice(["os", "sys", "re", "math", "json", "itertools", "functools", "collections", "string"])
        new = list(fi.lines)
        new.insert(last.end_lineno, "import " + mod)
        e = mk_edit(ctx, rel, fi, new, "add-import")
        if e:
            return e
    return None


def gen_newfile(ctx):
    if ctx.rng.random() < 0.65:
        names = ["newdialect", "newdialect", "newdialect", "mydialect", "example", "custom"]
        nm = ctx.rng.choice(names)
        rel = f"sqlglot/dialects/{nm}.py"
        cls = "NewDialect" if nm == "newdialect" else nm.capitalize()
        content = ("from __future__ import annotations\n\nfrom sqlglot.dialects.dialect import Dialect\n\n\n"
                   f"class {cls}(Dialect):\n    pass\n")
    else:
        nm = ctx.rng.choice(["test_extra", "test_extra", "test_misc", "test_sanity"])
        rel = f"tests/{nm}.py"
        body = ctx.rng.choice([
            'self.assertEqual(1 + 1, 2)',
            'self.assertEqual(parse_one("SELECT 1").sql(), "SELECT 1")',
            'self.assertIsNotNone(parse_one("SELECT a FROM b"))',
        ])
        content = ("import unittest\n\nfrom sqlglot import parse_one\n\n\nclass TestExtra(unittest.TestCase):\n"
                   f"    def test_extra(self):\n        {body}\n")
    if rel in ctx.fileset or rel in ctx.avoid:
        return None
    return {"files": {rel: content}, "desc": f"new file {rel}"}


def gen_rename(ctx):
    for _ in range(60):
        rel = ctx.pick(ctx.sg_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None:
            continue
        fns = [st.name for st in fi.tree.body if isinstance(st, ast.FunctionDef)
               and not (st.name.startswith("__") and st.name.endswith("__")) and st.name != "main"]
        if not fns:
            continue
        name = ctx.rng.choice(fns)
        try:
            toks = list(tokenize.generate_tokens(io.StringIO(fi.text).readline))
        except (tokenize.TokenError, SyntaxError):
            continue
        sig = [t for t in toks if t.type not in (tokenize.NL, tokenize.COMMENT, tokenize.NEWLINE,
                                                  tokenize.INDENT, tokenize.DEDENT)]
        edits, depth = [], 0
        for k, t in enumerate(sig):
            if t.type == tokenize.OP:
                if t.string in "([{":
                    depth += 1
                elif t.string in ")]}":
                    depth -= 1
            if t.type == tokenize.NAME and t.string == name:
                if k > 0 and sig[k - 1].type == tokenize.OP and sig[k - 1].string == ".":
                    continue
                if k + 1 < len(sig) and sig[k + 1].string == "=" and depth > 0:
                    continue
                edits.append(t.start)
        if len(edits) < 2 and ctx.rng.random() < 0.8:
            continue
        new = list(fi.lines)
        for row, col in sorted(edits, reverse=True):
            new[row - 1] = new[row - 1][:col] + name + "_new" + new[row - 1][col + len(name):]
        e = mk_edit(ctx, rel, fi, new, f"rename {name}->{name}_new ({len(edits)} sites)")
        if e:
            return e
    return None


def gen_default_arg(ctx):
    for _ in range(60):
        rel = ctx.pick(ctx.sg_py)
        if rel is None:
            return None
        if not ctx.info(rel).cands().get("default_arg"):
            continue
        e = code_edit_in(ctx, rel, ["default_arg"], tries=5)
        if e:
            return e
    return None


BASE_FILES = ["sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/tokens.py", "sqlglot/dialects/dialect.py",
              "sqlglot/parsers/base.py", "sqlglot/tokenizer_core.py"]


def ranges_of(fi, pred):
    rs = []
    for c in ast.walk(fi.tree):
        if isinstance(c, ast.ClassDef):
            for st in c.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None and pred(st):
                    rs.append((st.lineno, st.end_lineno))
    return rs


def in_ranges(ln, rs):
    return any(a <= ln <= z for a, z in rs)


def class_attr_edit(ctx, rel, pred, allow_delete=True, what="class-level"):
    fi = ctx.info(rel)
    if fi.tree is None:
        return None
    rs = ranges_of(fi, pred)
    if not rs:
        return None
    if allow_delete and ctx.rng.random() < 0.3:
        sp = [s for s in entry_spans(fi) if in_ranges(s[0], rs)]
        if sp:
            a, z = ctx.rng.choice(sp)
            new = list(fi.lines)
            del new[a - 1 : z]
            return mk_edit(ctx, rel, fi, new, what + " entry-delete")
    pool = [(s, c) for s in ALL_SUBS for c in fi.cands().get(s, []) if in_ranges(c[0], rs)]
    for _ in range(10):
        if not pool:
            return None
        s, (ln, make) = ctx.rng.choice(pool)
        edits = make(ctx.rng)
        if edits:
            e = mk_edit(ctx, rel, fi, apply_edits(fi.lines, edits), what + " " + s)
            if e:
                return e
    return None


def gen_base_flag(ctx):
    for _ in range(30):
        rel = ctx.pick(BASE_FILES, weighted=False)
        if rel is None:
            return None
        e = class_attr_edit(ctx, rel, lambda st: True, True, "base-class")
        if e:
            return e
    return None


def gen_arg_types(ctx):
    pool_files = [f for f in ctx.sg_py if f.startswith("sqlglot/expressions/") and not f.endswith("__init__.py")]

    def is_arg_types(st):
        t = st.targets[0] if isinstance(st, ast.Assign) else st.target
        return isinstance(t, ast.Name) and t.id == "arg_types" and isinstance(st.value, ast.Dict)

    for _ in range(40):
        rel = ctx.pick(pool_files)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None:
            continue
        r = ctx.rng.random()
        if r < 0.3:
            rs = ranges_of(fi, is_arg_types)
            keys = [k for st in ast.walk(fi.tree) if isinstance(st, ast.Dict) and in_ranges(st.lineno, rs)
                    for k in st.keys if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.lineno == k.end_lineno]
            if not keys:
                continue
            k = ctx.rng.choice(keys)
            s = fi.bl[k.lineno - 1][k.col_offset : k.end_col_offset].decode("utf-8")
            t = tweak_str(s, ctx.rng)
            if not t or t == s:
                continue
            e = mk_edit(ctx, rel, fi, apply_edits(fi.lines, [(k.lineno, k.col_offset, k.end_col_offset, t)]),
                        "arg_types key")
        else:
            e = class_attr_edit(ctx, rel, is_arg_types, r > 0.7, "arg_types")
        if e:
            return e
    return None


def gen_swap(ctx):
    for _ in range(60):
        rel = ctx.pick(ctx.sg_py + ctx.tests_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None:
            continue
        pool = []
        for _n, b in fi.bodies():
            for a, z in zip(b, b[1:]):
                if one_line_stmt(fi, a) and one_line_stmt(fi, z) and z.lineno == a.lineno + 1 \
                        and indent_of(fi.lines[a.lineno - 1]) == indent_of(fi.lines[z.lineno - 1]):
                    pool.append((a, z))
        if not pool:
            continue
        a, z = ctx.rng.choice(pool)
        new = list(fi.lines)
        new[a.lineno - 1], new[z.lineno - 1] = new[z.lineno - 1], new[a.lineno - 1]
        e = mk_edit(ctx, rel, fi, new, "swap-statements")
        if e:
            return e
    return None


NONPY = ["README.md", "pyproject.toml", "setup.py", "Makefile", "sqlglot/py.typed", "setup.cfg", "MANIFEST.in"]


def gen_nonpy(ctx):
    for _ in range(30):
        rel = ctx.pick(NONPY, weighted=False)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if rel == "sqlglot/py.typed":
            if fi.text == "":
                return {"files": {rel: "partial\n"}, "desc": f"{rel}: add 'partial' marker"}
            return {"files": {rel: None}, "desc": f"delete {rel}"}
        idx = [i for i, l in enumerate(fi.lines) if l.strip() and not l.startswith("\t")]
        if not idx:
            continue
        for _t in range(10):
            i = ctx.rng.choice(idx)
            nl = char_edit(fi.lines[i], ctx.rng, "repl", alnum_only=True)
            if nl and nl != fi.lines[i]:
                new = list(fi.lines)
                new[i] = nl
                e = mk_edit(ctx, rel, fi, new, "non-code")
                if e:
                    return e
    return None


def gen_comment_in_string(ctx):
    if ctx.rng.random() < 0.5:  # inside a SQL fixture
        for _ in range(40):
            rel = ctx.pick(ctx.sql_all)
            if rel is None:
                return None
            fi = ctx.info(rel)
            idx = [i for i in range(1, len(fi.lines) - 1) if fi.lines[i].strip() and fi.lines[i - 1].strip()]
            if not idx:
                continue
            i = ctx.rng.choice(idx)
            c = ctx.rng.choice(["-- ", "# "]) + ctx.rng.choice(COMMENT_TEXTS)
            new = list(fi.lines)
            new.insert(i, c)
            e = mk_edit(ctx, rel, fi, new, "comment-in-fixture")
            if e:
                return e
        return None
    for _ in range(60):
        rel = ctx.pick(ctx.sg_py + ctx.tests_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        spans = [(s, e) for s, e in string_rows(fi.text) if e - s >= 2]
        if not spans:
            continue
        s, e = ctx.rng.choice(spans)
        i = ctx.rng.randrange(s, e - 1)  # 0-based index of an interior line (row s+1 .. e-1)
        body = "\n".join(fi.lines[s - 1 : e])
        sqlish = re.search(r"\b(SELECT|FROM|CREATE|INSERT|WITH)\b", body, re.I) is not None
        c = ("-- " if sqlish else "# ") + ctx.rng.choice(COMMENT_TEXTS)
        new = list(fi.lines)
        new.insert(i, indent_of(fi.lines[i]) + c)
        r = mk_edit(ctx, rel, fi, new, "comment-in-string")
        if r:
            return r
    return None


MAGIC = ["# type: ignore", "# noqa", "# fmt: off", "# fmt: skip", "# pragma: no cover", "# noqa: E501"]


def gen_magic(ctx):
    for _ in range(60):
        rel = ctx.pick(ctx.sg_py + ctx.tests_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        m = ctx.rng.choice(MAGIC)
        new = list(fi.lines)
        if m == "# fmt: off" or ctx.rng.random() < 0.25:
            i = pick_comment_pos(fi, ctx.rng)
            if i is None:
                continue
            new.insert(i, indent_of(fi.lines[i]) + m)
        else:
            allr, _ = fi.rows()
            n = len(fi.lines) - 1
            idx = [i for i in range(n) if fi.lines[i].strip() and (i + 1) not in allr and "#" not in fi.lines[i]
                   and not fi.lines[i].rstrip().endswith("\\")]
            if not idx:
                continue
            i = ctx.rng.choice(idx)
            new[i] = new[i].rstrip() + "  " + m
        e = mk_edit(ctx, rel, fi, new, "magic-comment")
        if e:
            return e
    return None


def gen_import_edit(ctx):
    for _ in range(60):
        rel = ctx.pick(ctx.sg_py + ctx.tests_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None:
            continue
        pool = [n for n in ast.walk(fi.tree) if isinstance(n, ast.ImportFrom) and len(n.names) >= 2]
        if not pool:
            continue
        n = ctx.rng.choice(pool)
        new = list(fi.lines)
        if n.lineno == n.end_lineno:
            names = n.names
            seg = fi.bl[n.lineno - 1][n.col_offset : n.end_col_offset].decode("utf-8")
            m = re.match(r"(from\s+\S+\s+import\s+)([^()]*)$", seg)
            if not m:
                continue
            parts = [p.strip() for p in m.group(2).split(",")]
            if len(parts) != len(names):
                continue
            if ctx.rng.random() < 0.6:
                del parts[ctx.rng.randrange(len(parts))]
                what = "import drop name"
            else:
                i = ctx.rng.randrange(len(parts) - 1)
                parts[i], parts[i + 1] = parts[i + 1], parts[i]
                what = "import reorder"
            new = apply_edits(fi.lines, [(n.lineno, n.col_offset, n.end_col_offset, m.group(1) + ", ".join(parts))])
        else:
            cand = [a for a in n.names if a.lineno == a.end_lineno and fi.lines[a.lineno - 1].strip() in (a.name + ",", a.name)]
            cand = [a for a in cand if fi.lines[a.lineno - 1].strip().endswith(",")]
            if not cand:
                continue
            a = ctx.rng.choice(cand)
            del new[a.lineno - 1]
            what = "import drop name"
        e = mk_edit(ctx, rel, fi, new, what)
        if e:
            return e
    return None


def gen_loop_rare(ctx):
    for _ in range(80):
        rel = ctx.pick(ctx.sg_py)
        if rel is None:
            return None
        fi = ctx.info(rel)
        if fi.tree is None:
            continue
        par = fi.parents()
        pool = []
        for n in ast.walk(fi.tree):
            if not isinstance(n, ast.If) or n.body[0].lineno <= n.lineno:
                continue
            if isinstance(n.body[0], (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                continue
            t = n.test
            rare = (isinstance(t, ast.Compare) and any(isinstance(c, ast.Constant) and isinstance(c.value, (str, int))
                                                     for c in t.comparators)) or \
                   (isinstance(t, ast.Call) and isinstance(t.func, ast.Name) and t.func.id == "isinstance" and len(t.args) == 2)
            if not rare:
                continue
            q, in_fn = par.get(n), False
            while q is not None:
                if isinstance(q, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    in_fn = True
                    break
                q = par.get(q)
            if in_fn and fi.starts_line(n.body[0]):
                pool.append(n)
        if not pool:
            continue
        n = ctx.rng.choice(pool)
        i = n.body[0].lineno - 1
        ind = indent_of(fi.lines[i])
        new = list(fi.lines)
        new[i:i] = [ind + "while True:", ind + "    pass"]
        e = mk_edit(ctx, rel, fi, new, "loop-forever-on-rare-branch")
        if e:
            return e
    return None


# --------------------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------------------

# subkind -> dict(kind, fn, weight, profile, doc)
REG = {}


def reg(sub, kind, fn, weight, profile, doc, syntax=False):
    REG[sub] = dict(kind=kind, fn=fn, weight=weight, profile=profile, doc=doc, syntax=syntax)


for _s, _w in SAMPLE_CODE.items():
    reg(_s, "code", gen_code(_s), _w, "sample", CODE_DOC[_s])
reg("fixture_ins", "fixture", gen_fixture("ins"), 1.0, "sample", "insert one char in an optimizer fixture line")
reg("fixture_del", "fixture", gen_fixture("del"), 1.0, "sample", "delete one char in an optimizer fixture line")
reg("fixture_repl", "fixture", gen_fixture("repl"), 1.0, "sample", "replace one char in an optimizer fixture line")
reg("comment_add", "comment", gen_comment, 1.0, "sample", "add one `# ...` comment line in sqlglot/**.py")
_SAMPLE_MULTI_POOL = [("comment", gen_comment, 0.45), ("fixture", gen_fixture(None), 0.25)] + \
    [("code", gen_code(s), SAMPLE_CODE[s] * 0.15) for s in SAMPLE_CODE]
reg("multi_sample", "multi", gen_multi_from(_SAMPLE_MULTI_POOL), 1.0, "sample", "2-3 of comment/fixture/code in different files")

reg("test_expected", "test", gen_test_expected, 1.0, "novel", "change an expected string in a test file")
reg("test_helper", "test", gen_test_helper, 1.0, "novel", "code mutation in tests/helpers.py or tests/dialects/test_dialect.py")
reg("fixture_other", "fixture", gen_fixture_other, 1.5, "novel", "char edit in identity/pretty/partial/jsonpath/tpc-h/tpc-ds fixtures")
reg("fixture_meta", "fixture", gen_fixture_meta, 1.0, "novel", "edit fixture comment/meta lines (# dialect, # title, -- ...)")
reg("stmt_delete", "code", gen_stmt_delete, 1.0, "novel", "delete one statement line (pass if sole body)")
reg("func_delete", "code", gen_func_delete, 0.5, "novel", "delete a small function")
reg("entry_delete", "code", gen_entry_delete, 0.5, "novel", "delete a dict/list/set entry")
reg("doc_word", "doc", gen_doc_word, 0.6, "novel", "edit a word in a docstring")
reg("doc_doctest", "doc", gen_doc_doctest, 0.6, "novel", "change a doctest expected output")
reg("syntax_error", "syntax", gen_syntax, 1.0, "novel", "syntax error (paren/colon/indent/quote)", syntax=True)
reg("ws_trailing", "whitespace", gen_ws("trailing"), 0.3, "novel", "trailing whitespace")
reg("ws_blank_insert", "whitespace", gen_ws("blank_insert"), 0.3, "novel", "insert a blank line")
reg("ws_blank_delete", "whitespace", gen_ws("blank_delete"), 0.3, "novel", "delete a blank line")
reg("ws_reindent", "whitespace", gen_ws("reindent"), 0.3, "novel", "re-indent a continuation line")
reg("add_func", "code", gen_add_func, 0.5, "novel", "add an unused function")
reg("add_import", "code", gen_add_import, 0.5, "novel", "add an unused import")
reg("newfile", "newfile", gen_newfile, 0.8, "novel", "brand-new dialect module or test file")
reg("rename_func", "code", gen_rename, 0.8, "novel", "rename a module-level function and its uses in one file")
reg("default_arg", "code", gen_default_arg, 0.8, "novel", "change a default argument value")
reg("base_flag", "code", gen_base_flag, 1.0, "novel", "class-level flag/dict entry in parser/generator/tokens/dialect")
reg("arg_types", "code", gen_arg_types, 1.0, "novel", "arg_types entry change in sqlglot/expressions/*.py")
reg("swap_stmts", "code", gen_swap, 0.8, "novel", "swap two adjacent statements")
reg("nonpy", "nonpy", gen_nonpy, 1.0, "novel", "edit README/pyproject/setup.py/Makefile/py.typed/...")
reg("comment_in_string", "comment", gen_comment_in_string, 0.8, "novel", "comment inside a multi-line string or SQL fixture")
reg("magic_comment", "comment", gen_magic, 0.8, "novel", "# type: ignore / # noqa / # fmt: off ...")
reg("import_edit", "code", gen_import_edit, 0.8, "novel", "drop/reorder names on an import line")
reg("loop_rare", "code", gen_loop_rare, 0.25, "novel", "infinite loop on a rare branch (<=2% of set)")
_NOVEL_MULTI_POOL = [(s, d["fn"], d["weight"]) for s, d in REG.items()
                     if d["profile"] == "novel" and s not in ("loop_rare",)]
reg("multi_novel", "multi", gen_multi_from(_NOVEL_MULTI_POOL), 1.8, "novel", "2-3 novel edits in different files")

SAMPLE_KIND_SHARE = [("code", 0.53), ("fixture", 0.27), ("multi", 0.10), ("comment", 0.10)]

# --------------------------------------------------------------------------------------
# git materialisation
# --------------------------------------------------------------------------------------


class Repo:
    def __init__(self, src):
        self.tmp = tempfile.mkdtemp(prefix="mutgen_")
        self.w = os.path.join(self.tmp, "w")
        shutil.copytree(src, self.w, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.abbrev", "7")
        self.git("config", "core.filemode", "false")
        self.git("config", "diff.renames", "false")
        self.git("config", "user.name", "mutgen")
        self.git("config", "user.email", "mutgen@localhost")
        self.git("add", "-Af")
        self.git("commit", "-q", "-m", "base", "--no-gpg-sign")

    def git(self, *args):
        r = subprocess.run(["git", *args], cwd=self.w, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("git %s failed: %s" % (" ".join(args), r.stderr.decode(errors="replace")))
        return r.stdout

    def diff(self, files):
        try:
            for rel, content in files.items():
                p = os.path.join(self.w, rel)
                if content is None:
                    os.remove(p)
                else:
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "w", encoding="utf-8", newline="") as f:
                        f.write(content)
            self.git("add", "-Af")
            return self.git("diff", "--cached", "--no-color", "--no-ext-diff", "--no-renames").decode("utf-8")
        finally:
            self.git("reset", "-q", "--hard", "HEAD")
            self.git("clean", "-fdq")

    def check(self, patch_text):
        r = subprocess.run(["git", "apply", "--check", "-"], cwd=self.w, input=patch_text.encode(), capture_output=True)
        return r.returncode == 0

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------------------
# planning and main
# --------------------------------------------------------------------------------------


def largest_remainder(n, shares):
    raw = [n * s for _, s in shares]
    base = [int(x) for x in raw]
    rem = n - sum(base)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - base[i]), i))
    for i in order[:rem]:
        base[i] += 1
    return {k: b for (k, _), b in zip(shares, base)}


def subs_of(profile, kind=None):
    return [s for s, d in REG.items() if d["profile"] == profile and (kind is None or d["kind"] == kind)]


def pick_sub(rng, subs):
    return rng.choices(subs, [REG[s]["weight"] for s in subs])[0]


def plan(profile, n, rng, only):
    if only:
        return [only] * n
    out = []
    if profile in ("sample", "mixed"):
        ns = n if profile == "sample" else int(round(n * 0.6))
        quotas = largest_remainder(ns, SAMPLE_KIND_SHARE)
        for kind, q in quotas.items():
            for _ in range(q):
                out.append(pick_sub(rng, subs_of("sample", kind)))
    if profile in ("novel", "mixed"):
        nn = n if profile == "novel" else n - len(out)
        cap = int(0.02 * n)
        novel_subs = subs_of("novel")
        k = 0
        for _ in range(nn):
            s = pick_sub(rng, novel_subs)
            if s == "loop_rare":
                if k >= cap:
                    s = "stmt_delete"
                else:
                    k += 1
            out.append(s)
    rng.shuffle(out)
    return out


def list_kinds():
    print("%-18s %-11s %-7s %-6s %s" % ("subkind", "kind", "profile", "weight", "description"))
    for s, d in REG.items():
        print("%-18s %-11s %-7s %-6s %s" % (s, d["kind"], d["profile"], d["weight"], d["doc"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="output directory")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--profile", choices=["sample", "novel", "mixed"], default="sample")
    ap.add_argument("--kind", help="generate only this subkind")
    ap.add_argument("--src", default=DEFAULT_SRC, help="pinned sqlglot checkout (default /src)")
    ap.add_argument("--list-kinds", action="store_true")
    ap.add_argument("--verify", action="store_true", help="git apply --check every patch on a pristine copy")
    a = ap.parse_args(argv)
    if a.list_kinds:
        list_kinds()
        return 0
    if not a.out:
        ap.error("--out is required")
    if a.kind and a.kind not in REG:
        ap.error("unknown subkind %r; see --list-kinds" % a.kind)

    ctx = Ctx(a.src)
    master = random.Random(a.seed)
    seq = plan(a.profile, a.n, master, a.kind)
    ids, used = [], set()
    alphabet = string.ascii_lowercase + string.digits
    for _ in seq:
        while True:
            i = "".join(master.choices(alphabet, k=4))
            if i not in used:
                used.add(i)
                ids.append(i)
                break

    os.makedirs(os.path.join(a.out, "commits"), exist_ok=True)
    repo = Repo(a.src)
    meta, counts = {}, Counter()
    try:
        for idx, (sub, cid) in enumerate(zip(seq, ids)):
            cur = sub
            patch, edit = None, None
            for attempt in range(12):
                if attempt >= 6 and not a.kind:  # fall back to another subkind of the same profile
                    pool = subs_of(REG[sub]["profile"], REG[sub]["kind"] if attempt < 9 else None)
                    cur = pool[(idx + attempt) % len(pool)]
                ctx.rng = random.Random("%d:%d:%d" % (a.seed, idx, attempt))
                ctx.avoid = set()
                try:
                    edit = REG[cur]["fn"](ctx)
                except Exception as ex:  # a generator bug must not kill the batch
                    print("warn: %s raised %r" % (cur, ex), file=sys.stderr)
                    edit = None
                if not edit:
                    continue
                patch = repo.diff(edit["files"])
                if patch.strip():
                    break
                patch = None
            if not patch:
                print("warn: could not generate item %d (%s)" % (idx, sub), file=sys.stderr)
                continue
            with open(os.path.join(a.out, "commits", cid + ".patch"), "w", encoding="utf-8", newline="") as f:
                f.write(patch)
            meta[cid] = {"kind": REG[cur]["kind"], "subkind": cur, "files": sorted(edit["files"]),
                         "description": edit["desc"]}
            counts[cur] += 1
        with open(os.path.join(a.out, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1, sort_keys=True)
        bad = []
        if a.verify:
            for cid in meta:
                p = open(os.path.join(a.out, "commits", cid + ".patch"), encoding="utf-8", newline="").read()
                if not repo.check(p):
                    bad.append(cid)
            print("verify: %d patches, %d failed git apply --check %s" % (len(meta), len(bad), bad), file=sys.stderr)
    finally:
        repo.close()
    kc = Counter(m["kind"] for m in meta.values())
    print("wrote %d patches to %s; kinds=%s" % (len(meta), a.out, dict(kc)), file=sys.stderr)
    for s, c in sorted(counts.items()):
        print("  %-18s %d" % (s, c), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
