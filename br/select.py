"""Per-commit test selection.

For each commit:
  1. parse the patch and work out, from maps built at the pinned commit (line coverage, file reads),
     which tests could possibly be affected (the candidates);
  2. when it is cheap enough, apply the patch to a mirror of the sources and run candidates with our
     own unittest runner to see what really fails;
  3. emit the failing test plus every candidate we did not verify, packed over workers.

Anything unexpected widens the answer (up to the full suite); nothing here may guess low.
"""
from __future__ import annotations

import ast
import os
import re
import sys
import time

from br import adapters, classify, cmptypes, diffparse, entrymaps, maps, pack, slices

ROOT = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")
MAPS = os.environ.get("BR_MAPS", "/app/maps")
TMP = os.environ.get("BR_TMP", "/tmp/br")

# ---- tunables (generic thresholds; tuned on our own mutation benchmark) -----------------------
HEAVY_MS = int(os.environ.get("BR_HEAVY_MS", "2500"))          # tests at least this long are never run by us
VERIFY = os.environ.get("BR_VERIFY", "1") == "1"               # dynamic verification on/off
HEAVY_WHEN_CLEAN = os.environ.get("BR_HEAVY_WHEN_CLEAN", "include")  # include | exclude
MAX_VERIFY_CPU = float(os.environ.get("BR_MAX_VERIFY_CPU", "15"))     # per-commit cap on estimated verify CPU
VERIFY_FACTOR = float(os.environ.get("BR_VERIFY_FACTOR", "0.85"))
# Estimates are in seconds of the scoring machine, which runs about 3x faster than the recording machine.
EST_SCALE = float(os.environ.get("BR_EST_SCALE", "0.33"))     # verify if est cpu <= factor * static cost
DATA_STATIC = os.environ.get("BR_DATA_STATIC", "1") == "1"            # data-only patches: emit readers without verifying
WARM = os.environ.get("BR_WARM", "1") == "1"                          # pre-import unpatched sqlglot core in the parent
CLASS_NARROW = os.environ.get("BR_CLASS_NARROW", "1") == "1"          # class-body changes: only tests using the class
CLASS_SPAN = os.environ.get("BR_CLASS_SPAN", "0") == "1"              # also count tests running code inside the class body
CLASS_SPAN_MAX = int(os.environ.get("BR_CLASS_SPAN_MAX", "1500"))
REF_NARROW = os.environ.get("BR_REF_NARROW", "1") == "1"              # constant assignments: only tests running code that mentions the name
SLICE = os.environ.get("BR_SLICE", "1") == "1"                        # run only the affected fixture entries of loop tests
MAX_TEST_CPU = float(os.environ.get("BR_MAX_TEST_CPU", "1.5"))           # a single test costing more than this is not run by us
SAMPLE = os.environ.get("BR_SAMPLE", "1") == "1"                      # sample entries of loop tests too expensive to verify
SAMPLE_CPU = float(os.environ.get("BR_SAMPLE_CPU", "0.3"))
SAMPLE_TRUST = os.environ.get("BR_SAMPLE_TRUST", "1") == "1"          # drop a sampled test whenever its sample passes
SLICE_CAP = float(os.environ.get("BR_SLICE_CAP", "8"))               # most CPU spent on one exact slice
SLICE_SHARE = float(os.environ.get("BR_SLICE_SHARE", "0.75"))          # ... as a share of the cost of emitting the test
SAMPLE_MIN_MS = int(os.environ.get("BR_SAMPLE_MIN_MS", "10000"))       # only tests this slow are ever sampled
SAMPLE_MIN = int(os.environ.get("BR_SAMPLE_MIN", "3"))
ADAPT = os.environ.get("BR_ADAPT", "1") == "1"                        # cheap stand-ins for the slowest tests
MAX_NODES = "tests/test_parser.py::TestParser::test_max_nodes"
GEN_MEM = "tests/test_generator.py::TestGenerator::test_generator_memory"
EXECUTOR = "tests/test_executor.py::TestExecutor"
POOL_CLASSES = {EXECUTOR, "tests/test_optimizer.py::TestOptimizer"}
REC_SCALE = float(os.environ.get("BR_REC_SCALE", "0.85"))              # our CPU per recorded second, ordinary tests
NO_RUN = set() if ADAPT else {MAX_NODES}
DUCK1 = os.environ.get("BR_DUCK1", "0") == "1"
CONTINUE = os.environ.get("BR_CONTINUE", "1") == "1"                  # keep verifying after a failure when it pays
CONT_FACTOR = float(os.environ.get("BR_CONT_FACTOR", "0.6"))
REL_CPU = float(os.environ.get("BR_REL_CPU", "3.0"))
POOL_SPLIT = os.environ.get("BR_POOL_SPLIT", "1") == "1"
AUDIT = os.environ.get("BR_AUDIT", "0") == "1"                        # test aid: verify everything, report verdicts
AUDIT_CAP = float(os.environ.get("BR_AUDIT_CAP", "25"))
ALWAYS = os.environ.get("BR_ALWAYS", "0") == "1"
DEBUG = os.environ.get("BR_DEBUG", "0") == "1"
KEY_NARROW = os.environ.get("BR_KEY_NARROW", "0") == "1"              # table entries keyed by expression class
DATA_STATIC_MAX = float(os.environ.get("BR_DATA_STATIC_MAX", "5"))    # ...unless emitting the readers is expensive
DOC_NOOP = os.environ.get("BR_DOC_NOOP", "1") == "1"
HEADER_NARROW = os.environ.get("BR_HEADER_NARROW", "1") == "1"        # function-header changes: only tests entering the function
NO_HEADER_NARROW = {"sqlglot/executor/env.py"}                         # its functions' signatures are inspected, not just called
TOP_SLICE = os.environ.get("BR_TOP_SLICE", "1") == "1"                # slice loop tests for class-level changes too
ADD_NOOP = os.environ.get("BR_ADD_NOOP", "1") == "1"                  # unused module-level additions are no-ops
RUNNER_FILES = {"conftest.py", "pytest.ini", "tox.ini", "sitecustomize.py", "usercustomize.py", ".coveragerc"}
PYTEST_KEYS = {"addopts", "filterwarnings", "testpaths", "python_files", "python_classes", "python_functions",
               "markers", "xfail_strict", "timeout", "norecursedirs", "required_plugins", "pythonpath"}
ISEQ = os.environ.get("BR_ISEQ", "1") == "1"                          # prove is/== swaps equivalent from operand types
NEED_3P = ("tests/test_executor.py", "tests/test_optimizer.py")


class Full(Exception):
    """Give up on precision for this commit: run the whole suite."""


class Ctx:
    def __init__(self, tests, log):
        self.log = log
        self.tests = tests
        self.ids = [t["id"] for t in tests]
        self.num = {t["id"]: t["number"] for t in tests}
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        self.file_of = {t["number"]: t["file"] for t in tests}
        self.all = frozenset(self.ms)
        maps.load(MAPS)
        mids = maps.test_ids()
        # maps are indexed by the test numbering at record time; translate if tests.json differs
        self.same = mids == self.ids
        self.m2n = None if self.same else {i: self.num[x] for i, x in enumerate(mids) if x in self.num}
        self.unmapped = frozenset() if self.same else frozenset(self.num[x] for x in self.ids if x not in set(mids))
        try:
            cpu = maps.test_cpu()
        except Exception:
            cpu = None
        self.cpu = {}
        for n in self.all:
            c = None
            if cpu is not None:
                try:
                    i = n if self.same else mids.index(self.ids[n])
                    c = cpu[i]
                except Exception:
                    c = None
            self.cpu[n] = float(c) if c is not None else self.ms[n] / 1000 * 1.5 + 0.002
        try:
            self.unreliable = self.tr(maps.isolated_failures())
        except Exception:
            self.unreliable = frozenset()
        # A class's fixture/start-up cost is paid once per verification run, not once per test.
        self.cls_of = {t["number"]: t["id"].rsplit("::", 1)[0] for t in tests}
        self.fix = {}
        for n, c in self.cls_of.items():
            v = self.cpu[n]
            if c not in self.fix or v < self.fix[c]:
                self.fix[c] = v
        self.fixcost = dict(self.fix)      # what a class fixture costs once per verification run
        if ADAPT and EXECUTOR in self.fixcost:
            self.fixcost[EXECUTOR] = min(self.fixcost[EXECUTOR], 0.4)
        self.n_exec = sum(1 for c in self.cls_of.values() if c == EXECUTOR)
        self.slice_ok = True
        self.import_readers = set()
        self.last = ([], {})
        self.probes = []
        self.verifier = None
        self.mirror = None
        self.cpu_spent = 0.0

    def est(self, nums):
        """Estimated CPU seconds to run these tests in one child process."""
        classes = set()
        tot = 0.0
        for n in nums:
            c = self.cls_of[n]
            classes.add(c)
            tot += max(0.0005, self.cpu[n] - self.fix[c])
        # Isolated timings include one-off start-up work that a shared child pays once: scale down.
        return 0.05 + EST_SCALE * (tot + sum(self.fix[c] for c in classes))

    def tr(self, s):
        """Translate a set of map test numbers into tests.json numbers."""
        if self.same:
            return frozenset(s)
        return frozenset(self.m2n[i] for i in s if i in self.m2n)


def _read(path):
    with open(os.path.join(ROOT, path), "rb") as f:
        return f.read()


def _class_users(ctx, path, old_lines):
    """A statement in a class body runs at import time, so every importer of the module "covers" it.
    It can only matter to tests that use that class: tests that created an instance of it (or of a
    subclass, or of a class of the same dialect family) or ran code inside the class body.
    Returns None when this narrowing is not applicable."""
    try:
        out = set()
        seen = set()
        for ln in old_lines:
            key = maps.class_at(path, ln)
            if key is None or not maps.class_tracked(key):
                return None
            if key in seen:
                continue
            seen.add(key)
            for fam in maps.class_family(key):
                span = maps.class_span(fam)
                fpath = fam.split("::")[0]
                if span is None or span[1] - span[0] > CLASS_SPAN_MAX:
                    return None
                if CLASS_SPAN:
                    for l in range(span[0], span[1] + 1):
                        out |= maps.tests_for(fpath, l, "fn")
            out |= maps.tests_using_class(key)
        return ctx.tr(out)
    except Exception:
        return None


_CONST = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")
_REFS = None


def _refs():
    """Prebuilt index: constant-style name -> [(file, first line of statement, line of mention)]."""
    global _REFS
    if _REFS is None:
        import json
        import zlib
        with open(os.path.join(MAPS, "refs.json.z"), "rb") as f:
            d = json.loads(zlib.decompress(f.read()))
        _REFS = (d["files"], d["refs"])
    return _REFS


def _ref_users(ctx, path, old_text, old_lines):
    """The changed statements are plain constant assignments (NAME = value) at class or module level.
    Such a value can only matter to tests that run some code mentioning NAME. Find every line in the
    tree that mentions NAME (textually, so comments and strings count too) and return the tests that
    execute any of those lines. Returns None when the change is not of that simple form."""
    try:
        tree = _tree(path, old_text)
        names = set()
        own = set()
        todo = [tree]
        hit = set()
        while todo:
            node = todo.pop()
            for st in getattr(node, "body", []):
                lo, hi = st.lineno, getattr(st, "end_lineno", st.lineno)
                if isinstance(st, ast.ClassDef):
                    if any(lo <= ln <= hi for ln in old_lines):
                        # header/decorator lines of the class itself are not a constant assignment
                        if any(ln < st.body[0].lineno for ln in old_lines if lo <= ln <= hi):
                            return None
                        todo.append(st)
                    continue
                if not any(lo <= ln <= hi for ln in old_lines):
                    continue
                tgt = None
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    tgt = st.targets[0].id
                elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
                    tgt = st.target.id
                if tgt is None or not _CONST.match(tgt):
                    return None
                names.add(tgt)
                own.update(range(lo, hi + 1))
                hit.update(ln for ln in old_lines if lo <= ln <= hi)
        if not names or hit != set(old_lines):
            return None
        out = set()   # tests that run a mention inside a function
        top = set()   # tests that run a mention at import time (class/module level, one-line lambdas)
        same = False  # the changed file itself reads the name at import time
        readers = set()
        files, refs = _refs()
        recorded = {}
        for n in names:
            for fi, first, last in refs.get(n, ()):
                f = files[fi]
                if f == path and first in own:
                    continue
                rec = recorded.get(f)
                if rec is None:
                    rec = recorded[f] = maps.lines(f) if maps.known_file(f) else set()
                for k in range(first, last + 1):
                    if k in rec:
                        out |= maps.tests_for(f, k, "fn")
                        t = maps.tests_for(f, k, "top")
                        top |= t
                        if t and f == path:
                            same = True
                        if t and f != path and f.startswith("sqlglot/"):
                            readers.add(f)      # another module consumes the name while it is imported
        ctx.import_readers = readers
        return ctx.tr(out), ctx.tr(top), same
    except Exception:
        return None


def _header_users(ctx, path, old_text, old_lines):
    """All changed lines are in function headers (decorators, signature, default values). The function
    object changes, so only tests that enter the function can be affected. Function entry is recorded on
    the header lines. None when the change is not confined to function headers."""
    try:
        if path in NO_HEADER_NARROW:
            return None
        spans = _HEADERS.get(path)
        if spans is None:
            spans = []
            for node in ast.walk(_tree(path, old_text)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                    lo = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    hi = max(node.lineno, node.body[0].lineno - 1)
                    first = node.body[0]
                    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                        hi = max(hi, first.end_lineno)      # a docstring is reported with the header
                    spans.append((lo, hi))
            _HEADERS[path] = spans
        out = set()
        for ln in old_lines:
            hit = [(lo, hi) for lo, hi in spans if lo <= ln <= hi]
            if not hit:
                return None
            lo, hi = max(hit)                                # innermost
            for k in range(lo, hi + 1):
                out |= maps.tests_for(path, k, "fn")
        return ctx.tr(out)
    except Exception:
        return None


_HEADERS = {}
_EXPR = None


def _expr_classes():
    """Expression class name -> class key, for names that are unique among sqlglot/expressions classes."""
    global _EXPR
    if _EXPR is None:
        seen = {}
        for key in maps._load_cls()[0]:
            f, q = key.split("::")
            if f.startswith("sqlglot/expressions/") and "." not in q:
                seen.setdefault(q, []).append(key)
        _EXPR = {q: ks[0] for q, ks in seen.items() if len(ks) == 1}
    return _EXPR


def _key_users(ctx, old_text, old_lines):
    """The change is inside entries of a class-level table keyed by expression classes
    (e.g. TRANSFORMS = {exp.Foo: ...}). An entry only matters when its key is looked up, which needs an
    instance of that expression class: tests that created one. None when not of that form."""
    try:
        tree = ast.parse(old_text)
        lines = set(old_lines)
        keys = set()
        covered = set()
        todo = [tree]
        while todo:
            node = todo.pop()
            for st in getattr(node, "body", []):
                lo, hi = st.lineno, getattr(st, "end_lineno", st.lineno)
                if not any(lo <= ln <= hi for ln in lines):
                    continue
                if isinstance(st, ast.ClassDef):
                    todo.append(st)
                    continue
                val = st.value if isinstance(st, (ast.Assign, ast.AnnAssign)) else None
                if not isinstance(val, ast.Dict):
                    return None
                for k, v in zip(val.keys, val.values):
                    if k is None:           # **spread of another table
                        klo, khi = v.lineno, v.end_lineno
                        if any(klo <= ln <= khi for ln in lines):
                            return None
                        continue
                    klo, khi = k.lineno, v.end_lineno
                    hit = {ln for ln in lines if klo <= ln <= khi}
                    if not hit:
                        continue
                    if not (isinstance(k, ast.Attribute) and isinstance(k.value, ast.Name) and k.value.id == "exp"):
                        return None
                    ck = _expr_classes().get(k.attr)
                    if ck is None or not maps.class_tracked(ck):
                        return None
                    keys.add(ck)
                    covered |= hit
        if not keys or covered != lines:
            return None
        out = set()
        for ck in keys:
            out |= maps.tests_using_class(ck)
        return ctx.tr(out)
    except Exception:
        return None


def _only_is_eq_swaps(fp):
    """Cheap textual gate: every changed line pair differs only by `is`<->`==` / `is not`<->`!=`."""
    try:
        def norm(t):
            return " ".join(t.replace(" is not ", " != ").replace(" is ", " == ").split())
        any_pair = False
        for h in fp.hunks:
            old = [ln.text for ln in h.lines if ln.tag == "-"]
            new = [ln.text for ln in h.lines if ln.tag == "+"]
            if len(old) != len(new) or not old:
                return False
            for a, b in zip(old, new):
                if a == b or norm(a) != norm(b):
                    return False
                any_pair = True
        return any_pair
    except Exception:
        return False


def _strip_strings(tree):
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list):
            node.body = [st for st in body
                         if not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
                                 and isinstance(st.value.value, str))] or [ast.Pass()]
    return tree


def _only_unused_additions(path, old_text, new_text):
    """True when the new module is the old one plus inserted top-level statements that only bind new
    names nothing refers to: `import x` / `from x import y` of standard-library modules, plain functions,
    and constant assignments."""
    try:
        old = _tree(path, old_text)
        new = ast.parse(new_text)
        olds = [ast.dump(st) for st in old.body]
        added = []
        i = 0
        for st in new.body:
            if i < len(olds) and ast.dump(st) == olds[i]:
                i += 1
            else:
                added.append(st)
        if i != len(olds) or not added:
            return False
        used = _NAMES.get(path)
        if used is None:
            used = set()
            for node in ast.walk(old):
                if isinstance(node, ast.Name):
                    used.add(node.id)
                elif isinstance(node, ast.Attribute):
                    used.add(node.attr)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    used.add(node.name)
                elif isinstance(node, ast.alias):
                    used.add((node.asname or node.name).split(".")[0])
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                    used.add(node.value)
                elif isinstance(node, ast.arg):
                    used.add(node.arg)
            _NAMES[path] = used
        import builtins
        std = getattr(sys, "stdlib_module_names", frozenset())
        for st in added:
            names = []
            if isinstance(st, ast.Import):
                for a in st.names:
                    if a.name.split(".")[0] not in std:
                        return False
                    names.append((a.asname or a.name).split(".")[0])
            elif isinstance(st, ast.ImportFrom):
                if st.level or not st.module or st.module.split(".")[0] not in std or st.module == "__future__":
                    return False
                for a in st.names:
                    if a.name == "*":
                        return False
                    names.append(a.asname or a.name)
            elif isinstance(st, ast.FunctionDef):
                if st.decorator_list:
                    return False
                names.append(st.name)
            elif isinstance(st, ast.Assign):
                if not (len(st.targets) == 1 and isinstance(st.targets[0], ast.Name) and isinstance(st.value, ast.Constant)):
                    return False
                names.append(st.targets[0].id)
            else:
                return False
            for n in names:
                if n in used or n.startswith("__") or hasattr(builtins, n):
                    return False
                # Modules are star-imported into packages (sqlglot.expressions), so a new public name can
                # replace a name defined elsewhere: it must not occur anywhere in the tree.
                if n in _tree_words():
                    return False
        return True
    except Exception:
        return False


_WORDS = None


def _tree_words():
    """Every identifier-like word in the Python sources of the pinned tree."""
    global _WORDS
    if _WORDS is None:
        words = set()
        pat = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
        for top in ("sqlglot", "tests"):
            for d, _, files in os.walk(os.path.join(ROOT, top)):
                for f in files:
                    if f.endswith(".py"):
                        try:
                            with open(os.path.join(d, f), encoding="utf-8") as fh:
                                words.update(pat.findall(fh.read()))
                        except Exception:
                            pass
        _WORDS = words
    return _WORDS


def _bindings(tree):
    """Qualified names bound at module and class level (functions, classes, assignments, imports)."""
    out = set()
    todo = [("", tree)]
    while todo:
        prefix, node = todo.pop()
        for st in getattr(node, "body", []):
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add(prefix + st.name)
            elif isinstance(st, ast.ClassDef):
                out.add(prefix + st.name)
                todo.append((prefix + st.name + ".", st))
            elif isinstance(st, ast.Assign):
                for t in st.targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            out.add(prefix + n.id)
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)) and isinstance(st.target, ast.Name):
                out.add(prefix + st.target.id)
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                for a in st.names:
                    out.add(prefix + (a.asname or a.name).split(".")[0])
            elif isinstance(st, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                todo.append((prefix, st))
                for extra in ("orelse", "finalbody"):
                    sub = getattr(st, extra, None)
                    if sub:
                        todo.append((prefix, ast.Module(body=list(sub), type_ignores=[])))
                for h in getattr(st, "handlers", []) or []:
                    todo.append((prefix, h))
    return out


_BIND = {}


def _same_bindings(path, old_text, new_text):
    """True when the patch neither adds, removes nor renames a module/class-level name. A renamed or new
    name can override an inherited one or break importers, which no usage-based narrowing accounts for."""
    try:
        old = _BIND.get(path)
        if old is None:
            old = _BIND[path] = _bindings(_tree(path, old_text))
        return old == _bindings(ast.parse(new_text))
    except Exception:
        return False


_NAMES = {}
_TREES = {}
_SPANS = {}


def _tree(path, text):
    """Parsed pristine source, cached per file (do not mutate the result)."""
    t = _TREES.get(path)
    if t is None:
        t = _TREES[path] = ast.parse(text)
    return t


def _only_strings_differ(path, old_text, new_text, removed):
    """True when the two sources are the same program once bare string statements are removed."""
    try:
        if not removed:
            return False
        spans = _SPANS.get(path)
        if spans is None:
            spans = _SPANS[path] = [
                (st.lineno, st.end_lineno) for node in ast.walk(_tree(path, old_text))
                for st in (getattr(node, "body", None) if isinstance(getattr(node, "body", None), list) else ())
                if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)]
        # cheap gate: every edited line must sit inside a bare string statement of the old file
        if not all(any(lo <= ln <= hi for lo, hi in spans) for ln in removed):
            return False
        a = ast.dump(_strip_strings(ast.parse(old_text)))
        b = ast.dump(_strip_strings(ast.parse(new_text)))
        return a == b
    except Exception:
        return False


def analyze(ctx, text):
    """-> (candidates: set[int], certain: bool, changes: dict[path, bytes|None], notes: list[str])"""
    try:
        fps = diffparse.parse(text)
    except diffparse.PatchError as e:
        raise Full(f"unparseable patch: {e}")
    if not fps:
        raise Full("empty patch")
    cand = set(ctx.unmapped)
    probes = []    # changed modules whose import must be checked because candidates were narrowed
    impacts = []   # (path, old lines, "fn") for function-body changes; (path, [], None) = cannot be sliced
    data = {}      # changed non-Python file -> (old text, new text), or None when it cannot be sliced
    py_impact = bool(ctx.unmapped)
    certain = False
    changes = {}
    notes = []
    for fp in fps:
        old_p, new_p = fp.old_path, fp.new_path
        p = old_p or new_p
        if p is None:
            raise Full("no path")
        is_py = p.endswith(".py") or (new_p or "").endswith(".py")
        for q in {old_p, new_p} - {None}:
            base = q.rsplit("/", 1)[-1]
            # Files that change how every test is collected or imported: no narrowing is possible.
            if base in RUNNER_FILES or base.endswith(".pth"):
                raise Full(f"test-runner configuration changed: {q}")
            if q.endswith(".py") and "/" not in q and fp.status != "modify":
                raise Full(f"top-level module added or removed: {q}")   # may shadow a library module
            if q.endswith(".py") and fp.status == "add" and not os.path.isdir(os.path.join(ROOT, q.split("/")[0])):
                raise Full(f"new top-level package: {q}")
            if fp.binary and (q.startswith("sqlglot/") or q.startswith("tests/")) and fp.status != "delete":
                raise Full(f"binary file changed in the source tree: {q}")
            if q.endswith(".py") and fp.status == "add" and q.startswith("sqlglot/"):
                probes.append(q)
            if base in ("pyproject.toml", "setup.cfg") and _pytest_section_touched(fp):
                raise Full(f"pytest settings changed in {q}")
        if fp.binary:
            readers = ctx.tr(maps.tests_reading(p)) if old_p else frozenset()
            if readers or maps.known_file(p) or is_py:
                raise Full(f"binary change to {p}")
            notes.append(f"{p}: binary, unread")
            continue
        if fp.mode_only:
            notes.append(f"{p}: mode only")
            continue
        # old/new contents
        old_text = None
        if old_p is not None and fp.status != "add":
            try:
                old_text = _read(old_p).decode("utf-8")
            except FileNotFoundError:
                raise Full(f"{old_p} not in pinned tree")
            except UnicodeDecodeError:
                raise Full(f"{old_p} not utf-8")
        try:
            new_text = None if fp.status == "delete" else diffparse.apply(fp, old_text)
        except diffparse.PatchError as e:
            raise Full(f"patch does not apply to {p}: {e}")
        if fp.status == "delete":
            changes[old_p] = None
        else:
            if fp.status == "rename" and not fp.copy and old_p != new_p:
                changes[old_p] = None
            changes[new_p] = new_text.encode("utf-8")

        # which tests can this file change affect?
        c = set()
        if old_p is not None:
            c |= ctx.tr(maps.tests_reading(old_p))      # tests that open the file as data
        for q in {old_p, new_p} - {None}:
            d = os.path.dirname(q)
            if fp.status != "modify":
                c |= ctx.tr(maps.tests_reading("dir:" + d))  # tests that list the directory
        if is_py:
            if fp.status != "modify":
                if old_p is not None:
                    c |= ctx.tr(maps.tests_for_file(old_p, "any"))
                    certain = certain or bool(c)
                notes.append(f"{p}: {fp.status} ({len(c)})")
            else:
                if ISEQ and _only_is_eq_swaps(fp) and cmptypes.provably_equivalent(old_p, old_text, new_text):
                    # Only `is`/`==` (or `is not`/`!=`) were exchanged, and for every type that reaches these
                    # comparisons in any test the two operators are the same function: no behavior change.
                    notes.append(f"{p}: equivalent comparison")
                    cand |= c
                    continue
                imp = classify.classify(old_p, old_text, new_text, fp)
                k = imp.kind
                if (k == "whole_file" and ADD_NOOP and not diffparse.removed_old_lines(fp)
                        and _only_unused_additions(old_p, old_text, new_text)):
                    # Only new, unreferenced module-level names were added (an import of a standard-library
                    # module, a function, a constant). Existing code cannot see them; the import check below
                    # still confirms the module loads.
                    k = "noop"
                    probes.append(old_p)
                    notes.append(f"{p}: unused addition")
                elif (k in ("lines", "whole_file") and DOC_NOOP
                        and _only_strings_differ(old_p, old_text, new_text, diffparse.removed_old_lines(fp))):
                    # Only docstrings / bare string statements changed. Nothing in the tree reads __doc__
                    # (checked at the pinned commit), so no listed test can observe it.
                    k = "noop"
                    notes.append(f"{p}: docstring only")
                elif k == "noop":
                    pass
                if k == "noop":
                    if not notes or not notes[-1].startswith(p + ": "):
                        notes.append(f"{p}: noop")
                elif k == "lines":
                    level = imp.level
                    if level == "fn":
                        # Line events are not reported for code that sits on its definition line (one-line
                        # lambdas and defs). Such a line only shows import-time hits; treat it as import-level.
                        fn_hits = any(maps.tests_for(old_p, ln, "fn") for ln in imp.old_lines)
                        top_hits = any(maps.tests_for(old_p, ln, "top") for ln in imp.old_lines)
                        if not fn_hits and top_hits:
                            level = "top"
                    kind = "fn" if level == "fn" else "any"
                    anyc = set()
                    for ln in imp.old_lines:
                        anyc |= ctx.tr(maps.tests_for(old_p, ln, kind))
                    narrowed = None
                    header = None
                    if level == "top" and not _same_bindings(old_p, old_text, new_text):
                        level = "top!"      # names changed: every importer is a candidate, no narrowing
                    if level == "top" and HEADER_NARROW:
                        header = _header_users(ctx, old_p, old_text, imp.old_lines)
                    if header is not None:
                        narrowed = header
                    elif level == "top" and CLASS_NARROW:
                        narrowed = _class_users(ctx, old_p, imp.old_lines)
                    cc = set(narrowed) if narrowed is not None else set(anyc)
                    refs = None
                    if level == "top" and REF_NARROW and cc and header is None:
                        ctx.import_readers = set()
                        refs = _ref_users(ctx, old_p, old_text, imp.old_lines)
                        if refs is not None:
                            probes.extend(sorted(ctx.import_readers))
                            # only tests that run code mentioning the name; but if this file itself reads the
                            # name at import time (default arguments, later class-level code), every importer
                            cc = cc & (refs[0] | refs[1])
                            if refs[2]:
                                cc |= anyc
                    keyed = None
                    if level == "top" and KEY_NARROW and cc:
                        raw = diffparse.removed_old_lines(fp)   # the edited lines themselves, not the whole statement
                        keyed = _key_users(ctx, old_text, raw) if raw else None
                        if keyed is not None:
                            cc &= keyed
                    if level == "top" and (narrowed is not None or refs is not None or keyed is not None):
                        probes.append(old_p)   # narrowing assumes the module still imports: check that
                    c |= cc
                    if level == "fn":
                        impacts.append((old_p, sorted(imp.old_lines), "fn"))
                    elif TOP_SLICE and header is None and (narrowed is not None or refs is not None):
                        # An import-time statement can only matter to fixture entries that load this module:
                        # those are the entries whose own coverage contains the statement's lines.
                        impacts.append((old_p, sorted(imp.old_lines), "any"))
                    else:
                        impacts.append((old_p, [], None))
                    notes.append(f"{p}: lines/{level}{'/hdr' if header is not None else '/cls' if narrowed is not None else ''}"
                                 f"{'/ref' if refs is not None else ''}{'/key' if keyed is not None else ''} "
                                 f"{sorted(imp.old_lines)[:6]} -> {len(cc)}")
                elif k == "syntax_error":
                    c |= ctx.tr(maps.tests_for_file(old_p, "any"))
                    certain = certain or bool(c)
                    notes.append(f"{p}: syntax error ({len(c)})")
                else:  # whole_file or anything unexpected
                    c |= ctx.tr(maps.tests_for_file(old_p, "any"))
                    impacts.append((old_p, [], None))
                    notes.append(f"{p}: {k} {imp.reason[:40]} ({len(c)})")
        else:
            data[p] = (old_text, new_text) if fp.status == "modify" and old_text is not None else None
            notes.append(f"{p}: data file ({len(c)})")
        if is_py and c:
            py_impact = True
        cand |= c
    ctx.last = (impacts, data)
    ctx.probes = probes
    return cand, certain, changes, notes, py_impact


def _order(ctx, light, changes):
    """Cheap tests first, but tests whose file name matches a changed file's name go to the front."""
    stems = set()
    for p in changes:
        s = os.path.basename(p).rsplit(".", 1)[0].lower()
        if len(s) > 2:
            stems.add(s)

    def key(n):
        f = ctx.file_of[n].lower()
        related = any(s in f for s in stems)
        return (0 if related else 1, ctx.cpu[n])

    return sorted(light, key=key)


def _engine(ctx):
    if ctx.verifier is None:
        from br import engine, overlay
        os.makedirs(TMP, exist_ok=True)
        ctx.mirror = overlay.Mirror(ROOT, os.path.join(TMP, "m"))
        ctx.mirror.build()
        ctx.verifier = engine.Verifier(os.path.join(TMP, "m"), preload=("unittest", "json", "concurrent.futures"))
        if WARM:
            # Import the unpatched core once in the parent; a child re-imports everything if a patch touches it.
            ctx.verifier.warm(("sqlglot", "tests.helpers", "tests.dialects.test_dialect"))
    return ctx.verifier, ctx.mirror


def _cost(ctx, nums):
    if not nums:
        return 0.0
    return pack.cost_of_ms(pack.best([(n, ctx.ms[n]) for n in nums]), ctx.ms) / 1000


def _slice_plan(ctx, n, impacts, data):
    """How to run test n for this change: (fraction of the full test's cost, {fixture: entries} or None).
    None = run the whole test. Loop-style tests run only the fixture entries that execute a changed
    line (or that are themselves changed); everything else about the test is unchanged."""
    if not (SLICE and ctx.same and ctx.slice_ok):
        return 1.0, None
    try:
        files = entrymaps.sliceable(n)
        if not files or all(entrymaps.unused(n, fx) for fx in files):
            return 1.0, None
        sel = {fx: set() for fx in files}
        evidence = False
        for path, lines, kind in impacts:
            if kind is None:
                return 1.0, None
            for ln in lines:
                if entrymaps.needs_full(n, path, ln, kind):
                    return 1.0, None
                found = entrymaps.entries(n, path, ln, kind)
                if not found and n in ctx.tr(maps.tests_for(path, ln, kind)):
                    return 1.0, None   # the test runs this line but no recorded entry does: do not slice
                for fx, s in found.items():
                    sel[fx] |= s
            evidence = True
        for path, texts in data.items():
            fx = slices.fixture_arg(path)
            if fx is None or fx not in sel:
                # the file is not one this test loops over; if the test reads it some other way, run it whole
                if n in ctx.tr(maps.tests_reading(path)):
                    return 1.0, None
                continue
            if texts is None:
                return 1.0, None
            if entrymaps.unused(n, fx):
                evidence = True
                continue
            ch = slices.changed_entries(entrymaps.loader(n, fx), texts[0], texts[1])
            if ch is None:
                return 1.0, None
            sel[fx] |= ch
            evidence = True
        if not evidence:
            return 1.0, None
        full = entrymaps.full_cpu(n)
        frac = entrymaps.est_cpu(n, sel) / full if full > 0 else 1.0
        return min(1.0, max(0.02, frac)), sel
    except Exception:
        return 1.0, None


def _probe_fn(path):
    """Stand-in 'test': does the patched module still import?"""
    mod = path[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[:-9]

    def f():
        import importlib
        try:
            importlib.import_module("sqlglot")
            importlib.import_module(mod)
            return "pass"
        except BaseException:  # noqa: BLE001
            return "fail"
    return f


def _importers(ctx, paths):
    out = set()
    for p in paths:
        out |= ctx.tr(maps.tests_for_file(p, "any"))
    return out


def _probe_only(ctx, changes, deadline):
    """Candidates were narrowed below 'every importer' for these modules; that is only valid if the
    module still imports. Returns the tests to add (every importer of a module that no longer imports)."""
    probes = ctx.probes
    if not probes or not VERIFY:
        return _importers(ctx, probes) if probes else set()
    ver, mir = _engine(ctx)
    ids = ["probe::" + p for p in probes]
    try:
        mir.apply(changes)
        # Modules pre-imported in the parent stay valid unless the patch touches one of them (the engine
        # checks that) or a Python file under tests/ (helpers may be imported under other names).
        changed = None if any(p.startswith("tests/") and p.endswith(".py") for p in changes) else list(changes)
        res = ver.verify(ids, stop_on_fail=False, wall_s=max(1.0, min(20.0, deadline - time.time() - 3)),
                         changed=changed, specials={i: _probe_fn(p) for i, p in zip(ids, probes)})
    finally:
        mir.restore()
    ctx.cpu_spent += res.cpu_s
    return _importers(ctx, [p for i, p in zip(ids, probes) if res.results.get(i) != "pass"])


def _rel_set(ctx, n, others):
    """Members of `others` closely related to test n: same test class, or driven by the same fixture file."""
    try:
        def fixtures(t):
            return {f for f in (entrymaps.sliceable(t) or ()) if not entrymaps.unused(t, f)}
        fx = fixtures(n)
        same_class = ctx.cls_of[n] == EXECUTOR   # executor tests share tables and engine; optimizer tests do not
        out = set()
        for o in others:
            if o != n and ((same_class and ctx.cls_of[o] == ctx.cls_of[n]) or fx & fixtures(o)):
                out.add(o)
        return out
    except Exception:
        return set(others) - {n}


def _touches_executor_setup(ctx, changes):
    """Does the patch change a file under tests/ that some executor test reads or executes
    (its helpers, its own module, its data files)? Then the shared set-up may be affected."""
    try:
        for q in changes:
            if not q.startswith("tests/"):
                continue
            users = set(ctx.tr(maps.tests_reading(q)))
            if q.endswith(".py"):
                users |= ctx.tr(maps.tests_for_file(q, "any")) if maps.known_file(q) else set(ctx.all)
            if any(ctx.cls_of[n] == EXECUTOR for n in users):
                return True
        return False
    except Exception:
        return True


def _pytest_section_touched(fp):
    """Does the patch add, remove or change a line that mentions pytest, or sit inside a pytest section?"""
    try:
        section = ""
        for h in fp.hunks:
            for ln in h.lines:
                t = ln.text.strip()
                if t.startswith("[") and t.endswith("]"):
                    section = t.lower()
                if ln.tag in "+-" and ("pytest" in t.lower() or "pytest" in section):
                    return True
            # a hunk may start inside a section whose header is above the context: be careful with options
            if any(ln.tag in "+-" and ln.text.split("=")[0].strip().lower() in PYTEST_KEYS for ln in h.lines):
                return True
        return False
    except Exception:
        return True


def _gm_dialects(impacts, data):
    """Dialects whose own code contains a changed line; None = all dialects."""
    if data:
        return None
    out = set()
    for path, lines, kind in impacts:
        if kind is None:
            return None
        for ln in lines:
            d = adapters.generator_memory_dialects(path, ln, kind)
            if d is None:
                return None
            out.update(d)
    return sorted(out) if out else None


def _sample(ctx, n, sel, full_cost):
    """A loop test whose affected entries are too many to run: pick the cheapest affected entries up to a
    small CPU budget. Their outcome is evidence about the whole test (see decide)."""
    try:
        files = entrymaps.sliceable(n)
        if not files:
            return None
        full = entrymaps.full_cpu(n)
        if full <= 0:
            return None
        cpus = entrymaps.entry_cpu(n)
        pool = []
        for fx, cnt in files.items():
            if entrymaps.unused(n, fx):
                continue
            want = sel.get(fx) if sel is not None else None
            w = cpus.get(fx) or [1.0] * cnt
            for i in (sorted(want) if want else range(cnt)):
                wi = w[i] if i < len(w) else 1.0
                # entries with no recorded cost usually do not execute anything: poor evidence, try last
                pool.append((wi if wi > 0 else 1e9, fx, i))
        if not pool:
            return None
        pool.sort()
        out = {fx: set() for fx in files}
        cost = 0.0
        k = 0
        for _, fx, i in pool:
            out[fx].add(i)
            k += 1
            cost = full_cost * min(1.0, entrymaps.est_cpu(n, out) / full)
            if k >= SAMPLE_MIN and cost >= SAMPLE_CPU:
                break
        return out, cost
    except Exception:
        return None


def decide(ctx, commit, text, deadline):
    cand, certain, changes, notes, py_impact = analyze(ctx, text)
    impacts, data = ctx.last
    info = {"cand": len(cand), "notes": notes, "verified": 0, "cpu": 0.0, "how": ""}
    # Candidates were narrowed for these modules on the assumption that they still import. Check that
    # first, in a fresh process, before anything else has been imported.
    wide = _probe_only(ctx, changes, deadline) if ctx.probes else set()
    ctx.probes = []
    if wide:
        info["how"] = "module no longer imports"
        return cand | wide, info
    if not cand:
        info["how"] = "no candidates"
        return set(), info
    if certain:
        info["how"] = "certain"
        return cand, info
    if DATA_STATIC and not AUDIT and not py_impact and _cost(ctx, cand) <= DATA_STATIC_MAX:
        # A changed fixture almost always breaks the test that reads it; running it first only costs CPU.
        info["how"] = "data file change, static"
        return cand | _probe_only(ctx, changes, deadline), info
    static_cost = _cost(ctx, cand)
    lazy_ok = (ADAPT and not _touches_executor_setup(ctx, changes)
               and sum(1 for n in cand if ctx.cls_of[n] == EXECUTOR) < ctx.n_exec)

    # Per-test plan and estimated CPU to verify it.
    plan = {}
    vcost = {}
    never = set(cand) & ctx.unreliable       # cannot be verified by us
    sampled = set()                          # too expensive to verify fully: only some of their entries are run
    specials = {}                            # test id -> stand-in callable giving its verdict
    for n in cand:
        if n in never:
            continue
        tid = ctx.ids[n]
        if tid in NO_RUN:
            never.add(n)
            continue
        if ADAPT and tid == MAX_NODES:
            # a scaled-down copy of the test body (same structure, ~170x less work)
            plan[n] = None
            vcost[n] = 0.15
            specials[tid] = adapters.max_nodes_proxy
            continue
        if ADAPT and tid == GEN_MEM:
            # the test loops over dialects; run only the dialects whose code the change touches
            dl = _gm_dialects(impacts, data)
            plan[n] = None
            vcost[n] = 0.7 if dl is None else 0.03 + 0.07 * len(dl)
            specials[tid] = (lambda dl=dl: adapters.generator_memory_run(dl))
            continue
        frac, sel = _slice_plan(ctx, n, impacts, data)
        plan[n] = sel
        if ctx.cls_of[n] in POOL_CLASSES:
            # these run their work through a process pool when recorded; use our own measured CPU
            full_cost = max(0.0005, ctx.cpu[n] - ctx.fix[ctx.cls_of[n]]) * EST_SCALE
        else:
            # ordinary tests cost us a fraction of their recorded seconds on the scoring machine
            full_cost = max(0.0003, ctx.ms[n] / 1000 * REC_SCALE)
        vcost[n] = full_cost * frac
        cap = MAX_TEST_CPU
        if sel is not None:
            # an exact slice is worth much more CPU than a sample: allow up to a share of what emitting costs
            cap = max(cap, min(SLICE_CAP, SLICE_SHARE * (ctx.ms[n] / 1000 + 2)))
        if vcost[n] > cap:
            smp = _sample(ctx, n, sel, full_cost) if (SAMPLE and ctx.ms[n] >= SAMPLE_MIN_MS) else None
            if smp is None:
                never.add(n)
                del vcost[n]
            else:
                plan[n], vcost[n] = smp
                sampled.add(n)

    def est(vs):
        fix = ctx.fixcost if lazy_ok else ctx.fix
        return 0.02 + sum(vcost[n] for n in vs) + EST_SCALE * sum(
            fix[c] for c in {ctx.cls_of[n] for n in vs} if c in POOL_CLASSES)

    # Leave out of verification the tests that cost more to run than to simply emit.
    V = set(vcost)
    U = set(never)
    best = est(V) + _cost(ctx, U)
    for n in sorted(V - sampled, key=lambda n: -vcost[n])[:10]:
        j = est(V - {n}) + _cost(ctx, U | {n})
        if j < best - 1e-9:
            V.discard(n)
            U.add(n)
            best = j
    # Very large candidate sets: the pool-based optimizer/executor tests cost far more CPU to run than to
    # emit, while the ordinary tests are cheap to run. Consider verifying only the ordinary ones.
    pool = {n for n in V if ctx.cls_of[n] in POOL_CLASSES and n not in sampled}
    if POOL_SPLIT and len(pool) >= 20 and len(V) - len(pool) >= 50:
        j = est(V - pool) + _cost(ctx, U | pool)
        if j < best - 1.0:
            V -= pool
            U |= pool
            best = j
    gain = static_cost - _cost(ctx, U)
    e = est(V)
    too_much = (e > AUDIT_CAP) if AUDIT else (e > MAX_VERIFY_CPU or e > VERIFY_FACTOR * gain)
    if (not VERIFY or not V or too_much or time.time() + e * 4 + 5 > deadline):
        info["how"] = f"static (est {e:.1f}s vs gain {gain:.1f}s of {static_cost:.1f}s)"
        return cand | _probe_only(ctx, changes, deadline), info

    # Each sliced test gets its own fixture filter, switched on right before it runs. TestExecutor builds
    # its query lists once in setUpClass, so that class gets one filter: the union over its sliced tests.
    per_test = {}
    exec_filter = {}
    exec_whole = set()
    for n in V:
        sel = plan[n]
        is_exec = ctx.cls_of[n] == EXECUTOR
        if sel is None:
            if is_exec and SLICE and ctx.same and ctx.slice_ok:
                for fx in (entrymaps.sliceable(n) or ()):
                    if not entrymaps.unused(n, fx):
                        exec_whole.add(fx)
            continue
        per_test[ctx.ids[n]] = sel
        if is_exec:
            for fx, st in sel.items():
                if not entrymaps.unused(n, fx):
                    exec_filter.setdefault(fx, set()).update(st)
    for fx in exec_whole:
        exec_filter.pop(fx, None)
    filt = bool(per_test or exec_filter)

    ver, mir = _engine(ctx)
    order = _order(ctx, V, changes)
    ids = [ctx.ids[n] for n in order]
    # Tests that are expensive to emit but cheap to check run first, and a failure elsewhere never skips them.
    # Once any test fails the rest is emitted unverified, so nothing is worth running "regardless":
    # plain cheapest-first order, stop at the first failure.
    always = [ctx.ids[n] for n in order if ALWAYS and (ctx.ms[n] >= 1000 and vcost[n] < 0.7 * ctx.ms[n] / 1000)]
    if any(ctx.file_of[n] in NEED_3P for n in order):
        ver.ensure_preloaded(("duckdb", "pandas", "numpy"))
    timeouts = {ctx.ids[n]: max(3.0, 8 * ctx.cpu[n]) for n in order}
    wall = min(max(10.0, e * 12 + 10), max(1.0, deadline - time.time() - 3))

    # Lazy table loading skips set-up work the selected tests do not need. That is only faithful when the
    # patch cannot break the set-up itself: nothing under tests/ changed, and the change is not in code that
    # every executor test runs (set-up code is covered by all of them).
    lazy = lazy_ok and any(ctx.cls_of[n] == EXECUTOR for n in V)

    def prepare(h):
        if DUCK1 and "duckdb" in sys.modules:
            # CPU time counts every thread; one DuckDB thread does the same work with less overhead.
            duckdb = sys.modules["duckdb"]
            real_connect = duckdb.connect

            def connect(*a, **k):
                con = real_connect(*a, **k)
                try:
                    con.execute("SET threads=1")
                except Exception:
                    pass
                return con

            duckdb.connect = connect
        if filt:
            import unittest
            slices.install()
            orig_run = unittest.TestCase.run

            def run(self, result=None):
                cls = type(self)
                tid = "%s::%s::%s" % (cls.__module__.replace(".", "/") + ".py", cls.__qualname__, self._testMethodName)
                f = per_test.get(tid)
                if f is None or cls.__qualname__ == "TestExecutor":
                    slices.clear()
                else:
                    slices.set_filter(f)
                return orig_run(self, result)

            unittest.TestCase.run = run
        if lazy:
            adapters.install_executor_lazy()   # load TPC tables only when a test touches them
        if exec_filter and any(ctx.cls_of[n] == EXECUTOR for n in V):
            import tests.test_executor as te
            inner = te.TestExecutor.setUpClass.__func__

            def setup(cls):
                slices.set_filter(exec_filter)
                try:
                    inner(cls)
                finally:
                    slices.clear()

            te.TestExecutor.setUpClass = classmethod(setup)

    def go(run_ids, always_ids, stop):
        want = set(run_ids)
        return ver.verify(run_ids, stop_on_fail=stop, timeouts=timeouts,
                          wall_s=min(wall, max(1.0, deadline - time.time() - 3)), changed=changed,
                          prepare=prepare, always=always_ids,
                          specials={**{t: f for t, f in specials.items() if t in want},
                                    **{"probe::" + q: _probe_fn(q) for q in ctx.probes if "probe::" + q in want}})

    results = {}
    cpus = {}
    cpu = 0.0
    try:
        mir.apply(changes)
        # Warm modules stay valid only if the patch touches no module already imported and nothing under tests/.
        # Modules pre-imported in the parent stay valid unless the patch touches one of them (the engine
        # checks that) or a Python file under tests/ (helpers may be imported under other names).
        changed = None if any(p.startswith("tests/") and p.endswith(".py") for p in changes) else list(changes)
        # Phase 1: everything except the sampled tests; stop at the first failure.
        first = [i for i in ids if ctx.num[i] not in sampled] + ["probe::" + q for q in ctx.probes]
        res = go(first, [i for i in always if ctx.num[i] not in sampled], not AUDIT) if first else None
        if res is not None:
            results.update(res.results)
            cpus.update(res.cpus)
            cpu += res.cpu_s
            if res.died or res.error:
                ctx.log(f"   child problem: died={res.died} timed_out={res.timed_out} error={res.error} suspect={res.suspect}")
        broken = [q for q in ctx.probes if results.pop("probe::" + q, None) != "pass"]
        failed = {ctx.num[i] for i, o in results.items() if o != "pass" and i in ctx.num}
        passed = {ctx.num[i] for i, o in results.items() if o == "pass" and i in ctx.num}
        pending = set(order) - sampled - passed - failed
        # Phase 1b: a failure stopped the run. Everything not yet verified would be emitted; keep verifying
        # (without stopping) when that costs clearly less CPU than emitting those tests would.
        if CONTINUE and failed and pending and not broken:
            rest = est(pending)
            saved = _cost(ctx, failed | pending | U) - _cost(ctx, failed | U)
            if rest <= CONT_FACTOR * saved and rest <= MAX_VERIFY_CPU and time.time() + rest * 4 + 5 < deadline:
                more = [i for i in ids if ctx.num[i] in pending]
                res = go(more, more, False)
                results.update(res.results)
                cpus.update(res.cpus)
                cpu += res.cpu_s
                if res.died or res.error:
                    ctx.log(f"   child problem: died={res.died} timed_out={res.timed_out} error={res.error} suspect={res.suspect}")
                failed = {ctx.num[i] for i, o in results.items() if o != "pass" and i in ctx.num}
                passed = {ctx.num[i] for i, o in results.items() if o == "pass" and i in ctx.num}
                pending = set(order) - sampled - passed - failed
        # Phase 2: the sampled tests. One that is closely related to a test that already failed is kept
        # without running it. Related tests phase 1 did not reach are run now, so the decision rests on facts.
        second = []
        if sampled and not broken:
            for n in sorted(sampled, key=lambda n: vcost[n]):
                if SAMPLE_TRUST and _rel_set(ctx, n, failed):
                    continue
                if not SAMPLE_TRUST and (failed or pending):
                    continue
                second.append(n)
            extra = set()
            for n in list(second):
                rel = _rel_set(ctx, n, pending)
                if sum(vcost[o] for o in rel) > REL_CPU:
                    second.remove(n)        # too costly to settle: the sampled test is simply kept
                else:
                    extra |= rel
            if extra:
                # First settle the related tests (stop at the first failure): if one fails, the sampled
                # test is kept anyway and its sample need not run.
                res = go([ctx.ids[n] for n in sorted(extra, key=lambda n: vcost[n])], [], True)
                results.update(res.results)
                cpus.update(res.cpus)
                cpu += res.cpu_s
                failed = {ctx.num[i] for i, o in results.items() if o != "pass" and i in ctx.num}
                passed = {ctx.num[i] for i, o in results.items() if o == "pass" and i in ctx.num}
                pending = set(order) - sampled - passed - failed
                second = [n for n in second if not _rel_set(ctx, n, failed | pending)]
            run2 = [ctx.ids[n] for n in second]
            if run2:
                res = go(run2, run2, False)
                results.update(res.results)
                cpus.update(res.cpus)
                cpu += res.cpu_s
                if res.died or res.error:
                    ctx.log(f"   child problem: died={res.died} timed_out={res.timed_out} error={res.error} suspect={res.suspect}")
                failed = {ctx.num[i] for i, o in results.items() if o != "pass" and i in ctx.num}
                passed = {ctx.num[i] for i, o in results.items() if o == "pass" and i in ctx.num}
                pending = set(order) - sampled - passed - failed
    finally:
        mir.restore()
    ctx.cpu_spent += cpu
    info["cpu"] = cpu
    info["verified"] = len(results)
    if broken:
        info["how"] = "module no longer imports (or the check did not finish)"
        return failed | pending | U | sampled | _importers(ctx, broken), info
    if DEBUG and cpu > 2:
        top = sorted(cpus.items(), key=lambda kv: -kv[1])[:8]
        ctx.log("   cpu by test: " + ", ".join(f"{t.split('::')[-1]}={c:.2f}(est {vcost.get(ctx.num.get(t), 0):.2f})" for t, c in top)
                + f" | sum {sum(cpus.values()):.1f} of {cpu:.1f}")
    if AUDIT:
        # test aid: report every verdict so a script can compare them with the real outcomes
        ctx.log("AUDIT " + commit + " P " + ",".join(map(str, sorted(passed - sampled))) + " F "
                + ",".join(map(str, sorted(failed))) + " S " + ",".join(map(str, sorted(sampled & passed))))
    nsl = sum(1 for n in V if plan[n] is not None)
    # A sampled test is dropped only when its sample passed and no failed or unverified test is closely
    # related to it (same test class, or driven by the same fixture file).
    keep = set()
    for n in sampled:
        if n in failed:
            continue
        if n not in passed or _rel_set(ctx, n, failed | pending) or (not SAMPLE_TRUST and (failed or pending)):
            keep.add(n)
    if failed or pending or keep:
        info["how"] = (f"fail found ({len(failed)} failed, {len(pending)} unverified, {len(U)} not run, "
                       f"{nsl} sliced, {len(sampled)} sampled, {len(keep)} kept, est {e:.1f}s)")
        if DEBUG:
            ctx.log("   failed: " + ", ".join(ctx.ids[n].split("::")[-1] for n in sorted(failed))[:300]
                    + " | sampled passed: " + ", ".join(ctx.ids[n].split("::")[-1] for n in sorted(sampled & passed))
                    + " | kept: " + ", ".join(ctx.ids[n].split("::")[-1] for n in sorted(keep)))
        return failed | pending | U | keep, info
    info["how"] = f"clean ({len(passed)} passed, {len(U)} not run, {nsl} sliced, {len(sampled)} sampled, est {e:.1f}s)"
    return set(U), info


def run(data_dir, tests, commits, deadline, log):
    ctx = Ctx(tests, log)
    if SLICE:
        try:
            entrymaps.load(MAPS)
        except Exception as e:  # noqa: BLE001
            log("entry maps unavailable:", repr(e))
            ctx.slice_ok = False
    full = None
    answers = {}
    for c in commits:
        t0 = time.time()
        try:
            with open(os.path.join(data_dir, "commits", c + ".patch"), "rb") as f:
                text = f.read().decode("utf-8", errors="surrogateescape")
            sel, info = decide(ctx, c, text, deadline)
            answers[c] = pack.best([(n, ctx.ms[n]) for n in sorted(sel)])
            log(f"{c}: {len(sel)} tests | {info['how']} | cand {info['cand']} verified {info['verified']} "
                f"cpu {info['cpu']:.2f}s wall {time.time() - t0:.2f}s | {'; '.join(info['notes'])[:200]}")
        except Full as e:
            log(f"{c}: FULL SUITE ({e})")
        except Exception as e:  # noqa: BLE001
            log(f"{c}: FULL SUITE (internal error {e!r})")
    log(f"verify cpu total {ctx.cpu_spent:.2f}s")
    return answers
