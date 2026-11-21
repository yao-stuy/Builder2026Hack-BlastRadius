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
import time

from br import classify, diffparse, entrymaps, maps, pack, slices

ROOT = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")
MAPS = os.environ.get("BR_MAPS", "/app/maps")
TMP = os.environ.get("BR_TMP", "/tmp/br")

# ---- tunables (generic thresholds; tuned on our own mutation benchmark) -----------------------
HEAVY_MS = int(os.environ.get("BR_HEAVY_MS", "2500"))          # tests at least this long are never run by us
VERIFY = os.environ.get("BR_VERIFY", "1") == "1"               # dynamic verification on/off
HEAVY_WHEN_CLEAN = os.environ.get("BR_HEAVY_WHEN_CLEAN", "include")  # include | exclude
MAX_VERIFY_CPU = float(os.environ.get("BR_MAX_VERIFY_CPU", "20"))     # per-commit cap on estimated verify CPU
VERIFY_FACTOR = float(os.environ.get("BR_VERIFY_FACTOR", "0.8"))
EST_SCALE = float(os.environ.get("BR_EST_SCALE", "0.5"))     # verify if est cpu <= factor * static cost
DATA_STATIC = os.environ.get("BR_DATA_STATIC", "1") == "1"            # data-only patches: emit readers without verifying
WARM = os.environ.get("BR_WARM", "1") == "1"                          # pre-import unpatched sqlglot core in the parent
CLASS_NARROW = os.environ.get("BR_CLASS_NARROW", "1") == "1"          # class-body changes: only tests using the class
CLASS_SPAN_MAX = int(os.environ.get("BR_CLASS_SPAN_MAX", "1500"))
REF_NARROW = os.environ.get("BR_REF_NARROW", "1") == "1"              # constant assignments: only tests running code that mentions the name
SLICE = os.environ.get("BR_SLICE", "1") == "1"                        # run only the affected fixture entries of loop tests
MAX_TEST_CPU = float(os.environ.get("BR_MAX_TEST_CPU", "4"))           # a single test costing more than this is not run by us
SAMPLE = os.environ.get("BR_SAMPLE", "1") == "1"                      # sample entries of loop tests too expensive to verify
SAMPLE_CPU = float(os.environ.get("BR_SAMPLE_CPU", "1.5"))
SAMPLE_MIN = int(os.environ.get("BR_SAMPLE_MIN", "6"))
NO_RUN = {"tests/test_parser.py::TestParser::test_max_nodes"}
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
        self.slice_ok = True
        self.last = ([], {})
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
        tree = ast.parse(old_text)
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
        out = set()
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
                        out |= maps.tests_for(f, k, "any")
        return ctx.tr(out)
    except Exception:
        return None


def analyze(ctx, text):
    """-> (candidates: set[int], certain: bool, changes: dict[path, bytes|None], notes: list[str])"""
    try:
        fps = diffparse.parse(text)
    except diffparse.PatchError as e:
        raise Full(f"unparseable patch: {e}")
    if not fps:
        raise Full("empty patch")
    cand = set(ctx.unmapped)
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
                imp = classify.classify(old_p, old_text, new_text, fp)
                k = imp.kind
                if k == "noop":
                    notes.append(f"{p}: noop")
                elif k == "lines":
                    kind = "fn" if imp.level == "fn" else "any"
                    narrowed = None
                    if imp.level == "top" and CLASS_NARROW:
                        narrowed = _class_users(ctx, old_p, imp.old_lines)
                    if narrowed is not None:
                        cc = set(narrowed)
                    else:
                        cc = set()
                        for ln in imp.old_lines:
                            cc |= ctx.tr(maps.tests_for(old_p, ln, kind))
                    refs = None
                    if imp.level == "top" and REF_NARROW and cc:
                        refs = _ref_users(ctx, old_p, old_text, imp.old_lines)
                        if refs is not None:
                            cc &= refs
                    c |= cc
                    impacts.append((old_p, sorted(imp.old_lines), "fn") if imp.level == "fn" else (old_p, [], None))
                    notes.append(f"{p}: lines/{imp.level}{'/cls' if narrowed is not None else ''}"
                                 f"{'/ref' if refs is not None else ''} {sorted(imp.old_lines)[:6]} -> {len(cc)}")
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


def decide_v1(ctx, commit, text, deadline):
    cand, certain, changes, notes, py_impact = analyze(ctx, text)
    info = {"cand": len(cand), "notes": notes, "verified": 0, "cpu": 0.0, "how": ""}
    if not cand:
        info["how"] = "no candidates"
        return set(), info
    if certain:
        info["how"] = "certain"
        return cand, info
    if DATA_STATIC and not py_impact:
        # A changed fixture almost always breaks the test that reads it; running it first only costs CPU.
        info["how"] = "data file change, static"
        return cand, info
    heavy = {n for n in cand if ctx.ms[n] >= HEAVY_MS}
    unrel = (cand - heavy) & ctx.unreliable
    light = cand - heavy - unrel
    static_cost = _cost(ctx, cand)
    # What we would still have to emit if every light candidate passes.
    residual = set(unrel) | (heavy if HEAVY_WHEN_CLEAN == "include" else set())
    gain = static_cost - _cost(ctx, residual)
    est = ctx.est(light)
    if (not VERIFY or not light or est > MAX_VERIFY_CPU or est > VERIFY_FACTOR * gain
            or time.time() + est * 2 + 5 > deadline):
        info["how"] = f"static (est {est:.1f}s vs gain {gain:.1f}s of {static_cost:.1f}s)"
        return cand, info

    ver, mir = _engine(ctx)
    order = _order(ctx, light, changes)
    ids = [ctx.ids[n] for n in order]
    if any(ctx.file_of[n] in NEED_3P for n in order):
        ver.ensure_preloaded(("duckdb", "pandas", "numpy"))
    timeouts = {ctx.ids[n]: max(3.0, 8 * ctx.cpu[n]) for n in order}
    wall = min(max(10.0, est * 6 + 10), max(1.0, deadline - time.time() - 3))
    try:
        mir.apply(changes)
        # Warm modules stay valid only if the patch touches no module already imported and nothing under tests/.
        changed = None if any(p.startswith("tests/") for p in changes) else list(changes)
        res = ver.verify(ids, stop_on_fail=True, timeouts=timeouts, wall_s=wall, changed=changed)
    finally:
        mir.restore()
    ctx.cpu_spent += res.cpu_s
    info["cpu"] = res.cpu_s
    info["verified"] = len(res.results)
    passed = {ctx.num[i] for i, o in res.results.items() if o == "pass"}
    failed = {ctx.num[i] for i, o in res.results.items() if o != "pass"}
    pending = set(order) - passed - failed  # not run, or in progress when the child died
    if failed or pending:
        info["how"] = f"fail found ({len(failed)} failed, {len(pending)} unverified)"
        return failed | pending | heavy | unrel, info
    sel = set(unrel)
    if heavy and HEAVY_WHEN_CLEAN == "include":
        sel |= heavy
    info["how"] = f"clean ({len(passed)} passed, heavy {len(heavy)} {HEAVY_WHEN_CLEAN})"
    return sel, info


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
                for fx, s in entrymaps.entries(n, path, ln, kind).items():
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
                pool.append((w[i] if i < len(w) else 1.0, fx, i))
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
    if not cand:
        info["how"] = "no candidates"
        return set(), info
    if certain:
        info["how"] = "certain"
        return cand, info
    if DATA_STATIC and not py_impact:
        # A changed fixture almost always breaks the test that reads it; running it first only costs CPU.
        info["how"] = "data file change, static"
        return cand, info
    static_cost = _cost(ctx, cand)

    # Per-test plan and estimated CPU to verify it.
    plan = {}
    vcost = {}
    never = set(cand) & ctx.unreliable       # cannot be verified by us
    sampled = set()                          # too expensive to verify fully: only some of their entries are run
    for n in cand:
        if n in never:
            continue
        if ctx.ids[n] in NO_RUN:
            never.add(n)
            continue
        frac, sel = _slice_plan(ctx, n, impacts, data)
        plan[n] = sel
        full_cost = max(0.0005, ctx.cpu[n] - ctx.fix[ctx.cls_of[n]]) * EST_SCALE
        vcost[n] = full_cost * frac
        if vcost[n] > MAX_TEST_CPU:
            smp = _sample(ctx, n, sel, full_cost) if SAMPLE else None
            if smp is None:
                never.add(n)
                del vcost[n]
            else:
                plan[n], vcost[n] = smp
                sampled.add(n)

    def est(vs):
        return 0.05 + sum(vcost[n] for n in vs) + EST_SCALE * sum(ctx.fix[c] for c in {ctx.cls_of[n] for n in vs})

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
    gain = static_cost - _cost(ctx, U)
    e = est(V)
    if (not VERIFY or not V or e > MAX_VERIFY_CPU or e > VERIFY_FACTOR * gain
            or time.time() + e * 2 + 5 > deadline):
        info["how"] = f"static (est {e:.1f}s vs gain {gain:.1f}s of {static_cost:.1f}s)"
        return cand, info

    # Fixture filters for sliced tests; a file some test needs whole stays unfiltered.
    filt = {}
    whole = set()
    for n in V:
        sel = plan[n]
        if sel is None:
            fl = entrymaps.sliceable(n) if (SLICE and ctx.same and ctx.slice_ok) else None
            for fx in (fl or ()):
                if not entrymaps.unused(n, fx):
                    whole.add(fx)
        else:
            for fx, s in sel.items():
                filt.setdefault(fx, set()).update(s)
    for fx in whole:
        filt.pop(fx, None)

    ver, mir = _engine(ctx)
    order = _order(ctx, V, changes)
    ids = [ctx.ids[n] for n in order]
    # Tests that are expensive to emit but cheap to check run first, and a failure elsewhere never skips them.
    always = [ctx.ids[n] for n in order if n in sampled or (ctx.ms[n] >= 1000 and vcost[n] < 0.7 * ctx.ms[n] / 1000)]
    if any(ctx.file_of[n] in NEED_3P for n in order):
        ver.ensure_preloaded(("duckdb", "pandas", "numpy"))
    timeouts = {ctx.ids[n]: max(3.0, 8 * ctx.cpu[n]) for n in order}
    wall = min(max(10.0, e * 6 + 10), max(1.0, deadline - time.time() - 3))

    def prepare(h):
        if filt or SLICE:
            slices.install()
            slices.set_filter(filt)

    try:
        mir.apply(changes)
        # Warm modules stay valid only if the patch touches no module already imported and nothing under tests/.
        changed = None if any(p.startswith("tests/") for p in changes) else list(changes)
        res = ver.verify(ids, stop_on_fail=True, timeouts=timeouts, wall_s=wall, changed=changed,
                         prepare=prepare if (filt and SLICE) else None, always=always)
    finally:
        mir.restore()
    ctx.cpu_spent += res.cpu_s
    info["cpu"] = res.cpu_s
    info["verified"] = len(res.results)
    passed = {ctx.num[i] for i, o in res.results.items() if o == "pass"}
    failed = {ctx.num[i] for i, o in res.results.items() if o != "pass"}
    pending = set(order) - passed - failed  # not run, or in progress when the child died
    nsl = sum(1 for n in V if plan[n] is not None)
    if failed or pending:
        # A sampled test that passed its sample is still emitted when something else failed.
        info["how"] = (f"fail found ({len(failed)} failed, {len(pending)} unverified, {len(U)} not run, "
                       f"{nsl} sliced, {len(sampled)} sampled, est {e:.1f}s)")
        return failed | pending | U | sampled, info
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
