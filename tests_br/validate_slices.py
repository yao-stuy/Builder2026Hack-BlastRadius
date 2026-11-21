"""Validate slice verdict == full-test verdict (run in the lab container).

usage: python tests_br/validate_slices.py [--jobs 5] [--maps /repo/maps] [--mutants 40] [--seed 1]
                                          [--only ID ...] [--heavy] [--out /scratch/validate.json]
For every patch and every sliceable candidate test: run (a) the slice and (b) the full test against the
patched tree (forked fresh processes, harness), compare pass/fail.  For fixture edits the slice is the
changed entries (br.slices.changed_entries); for code edits it is the entries whose recorded coverage
contains a changed OLD line (br.entrymaps), unless the line is in the test's base coverage (then the slice
is the whole test, reported as 'full').  Fixture samples are also run on the pristine tree.
"""
import argparse
import json
import os
import random
import resource
import select
import shutil
import signal
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from br import classify, diffparse, entrymaps, slices  # noqa: E402
from br.overlay import Mirror  # noqa: E402

SRC = "/src"
SKIP_FULL = ("test_max_nodes", "test_execute_tpcds")
FIXTURE_SAMPLES = ["2vdj", "6vvp", "egys", "f4bn", "gbhk", "mthh", "nwnx", "st8f"]
CODE_SAMPLES = ["2jrq", "535z", "j6uz", "rszg", "5y72"]


def child_run(wfd, root, tid, filt):
    try:
        os.setsid()
        dn = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(dn, fd)
        from br import harness
        harness.setup(root)
        slices.install()
        if filt is not None:
            slices.set_filter(filt)
        res = harness.run([tid])
        out = {"o": res[0][1]}
    except BaseException as e:  # noqa: BLE001
        out = {"o": "fail", "err": repr(e)}
    os.write(wfd, json.dumps(out).encode())
    os._exit(0)


def par(jobs, tasks, timeout=900):
    """tasks: list of (key, root, tid, filt). returns {key: (outcome, cpu)}"""
    pending = list(tasks)
    running = {}
    res = {}
    while pending or running:
        while pending and len(running) < jobs:
            key, root, tid, filt = pending.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                child_run(w, root, tid, filt)
            os.close(w)
            running[r] = [pid, key, time.time(), b""]
        ready, _, _ = select.select(list(running), [], [], 1.0)
        for fd in list(running):
            pid, key, st, buf = running[fd]
            fin = False
            if fd in ready:
                c = os.read(fd, 65536)
                if c:
                    running[fd][3] += c
                else:
                    fin = True
            if not fin and time.time() - st > timeout:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except Exception:
                    pass
                fin = True
                running[fd][3] = b'{"o":"fail","err":"timeout"}'
            if fin:
                os.close(fd)
                try:
                    _, _, ru = os.wait4(pid, 0)
                    cpu = ru.ru_utime + ru.ru_stime
                except ChildProcessError:
                    cpu = 0.0
                try:
                    o = json.loads(running[fd][3])["o"]
                except Exception:
                    o = "fail"
                res[key] = (o, cpu)
                del running[fd]
    return res


def read_src(rel):
    p = os.path.join(SRC, rel)
    return open(p, encoding="utf-8", newline="").read() if os.path.exists(p) else None


def prepare(patch_path):
    """-> dict(changes={rel: bytes}, files=[(rel, old, new, fp)]) or None"""
    fps = diffparse.parse(open(patch_path, encoding="utf-8", errors="surrogateescape").read())
    changes = {}
    files = []
    for fp in fps:
        rel = fp.path
        old = read_src(fp.old_path) if fp.old_path else None
        new = diffparse.apply(fp, old)
        changes[rel] = new.encode("utf-8", "surrogateescape")
        files.append((rel, old, new, fp))
    return {"changes": changes, "files": files}


def plan(info, tests_with_maps):
    """-> list of (test_number, slice_filter | 'full' | None(no hit), note)"""
    plans = {}
    for rel, old, new, fp in info["files"]:
        fx = slices.fixture_arg(rel)
        if fx is not None:
            for n in tests_with_maps:
                fl = entrymaps.sliceable(n)
                if fx not in fl or entrymaps.unused(n, fx):
                    continue
                ce = slices.changed_entries(entrymaps.loader(n, fx), old or "", new or "")
                p = plans.setdefault(n, {"full": False, "sel": {}})
                if ce is None:
                    p["full"] = True
                else:
                    p["sel"].setdefault(fx, set()).update(ce)
        elif rel.endswith(".py"):
            imp = classify.classify(rel, old or "", new or "", fp)
            if imp.kind == "noop":
                continue
            if imp.kind != "lines" or not imp.old_lines:
                for n in tests_with_maps:
                    plans.setdefault(n, {"full": False, "sel": {}})["full"] = True
                continue
            kind = {"fn": "fn", "top": "top"}.get(imp.level, "any")
            for n in tests_with_maps:
                p = plans.get(n)
                for ln in imp.old_lines:
                    if entrymaps.needs_full(n, rel, ln, kind):
                        plans.setdefault(n, {"full": False, "sel": {}})["full"] = True
                        continue
                    for fx, s in entrymaps.entries(n, rel, ln, kind).items():
                        plans.setdefault(n, {"full": False, "sel": {}})["sel"].setdefault(fx, set()).update(s)
        else:
            pass
    out = []
    for n, p in plans.items():
        if p["full"]:
            out.append((n, "full", ""))
        elif p["sel"]:
            filt = {fx: set() for fx in entrymaps.fixture_files(n)}
            for fx, s in p["sel"].items():
                filt[fx] = s
            out.append((n, filt, ""))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=5)
    ap.add_argument("--maps", default="/repo/maps")
    ap.add_argument("--mutants", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--heavy", action="store_true")
    ap.add_argument("--out", default="/scratch/validate.json")
    a = ap.parse_args()

    entrymaps.load(a.maps)
    tests = json.load(open("/kit/sample/tests.json"))["tests"]
    tid_of = {t["number"]: t["id"] for t in tests}
    cand_tests = sorted(int(n) for n in entrymaps._idx["tests"])
    if not os.path.isdir("/tmp/s/sqlglot"):
        shutil.copytree(SRC, "/tmp/s", ignore=shutil.ignore_patterns(".git", "__pycache__"))

    patches = []  # (id, path, group)
    for c in FIXTURE_SAMPLES:
        patches.append((c, f"/kit/sample/commits/{c}.patch", "sample-fixture"))
    for c in CODE_SAMPLES:
        patches.append((c, f"/kit/sample/commits/{c}.patch", "sample-code"))
    meta = json.load(open("/scratch/bench/trial_sample/meta.json"))
    rng = random.Random(a.seed)
    fx = sorted(k for k, v in meta.items() if v["kind"] == "fixture")
    cd = sorted(k for k, v in meta.items() if v["kind"] == "code")
    mu = sorted(k for k, v in meta.items() if v["kind"] == "multi" and all(f.endswith(".py") for f in v["files"]))
    nfx = a.mutants * 3 // 8
    ncd = a.mutants - nfx - 5
    pick = rng.sample(fx, nfx) + rng.sample(cd, ncd) + rng.sample(mu, min(5, len(mu)))
    for c in pick:
        patches.append((c, f"/scratch/bench/trial_sample/commits/{c}.patch", "mutant-" + meta[c]["kind"]))
    if a.only:
        patches = [p for p in patches if p[0] in a.only]

    work = []  # per (patch, test) records
    tasks = []
    mirrors = {}
    t_plan = time.time()
    for pid, path, group in patches:
        try:
            info = prepare(path)
            pl = plan(info, cand_tests)
        except Exception as e:  # noqa: BLE001
            print(f"[{pid}] plan error {e!r}", flush=True)
            continue
        if not pl:
            print(f"[{pid}] no sliceable candidate", flush=True)
            continue
        root = f"/tmp/m_{pid}"
        m = Mirror("/tmp/s", root)
        m.build()
        m.apply(info["changes"])
        mirrors[pid] = m
        for n, filt, note in pl:
            tid = tid_of[n]
            heavy = any(s in tid for s in SKIP_FULL) and not a.heavy
            if filt == "full":
                work.append({"patch": pid, "group": group, "test": n, "mode": "full", "skipped": heavy})
                continue
            rec = {"patch": pid, "group": group, "test": n, "mode": "slice", "skipped": heavy,
                   "filter": {k: sorted(v) for k, v in filt.items() if v}}
            work.append(rec)
            tasks.append(((pid, n, "slice"), root, tid, filt))
            if not heavy:
                tasks.append(((pid, n, "full"), root, tid, None))
            if group == "sample-fixture":
                tasks.append(((pid, n, "pristine"), "/tmp/s", tid, filt))
    print(f"planned {len(work)} (patch,test) pairs, {len(tasks)} runs in {time.time() - t_plan:.0f}s", flush=True)
    # heavy tests first, to balance
    tasks.sort(key=lambda t: 0 if any(s in tid_of_run(t, tid_of) for s in SKIP_FULL) else 1)
    t0 = time.time()
    res = par(a.jobs, tasks)
    wall = time.time() - t0
    for m in mirrors.values():
        m.cleanup()

    dis = []
    cmp_n = 0
    cpu_slice = cpu_full = 0.0
    rows = []
    for w in work:
        k = (w["patch"], w["test"])
        if w["mode"] == "full":
            w["verdict"] = "full-fallback"
            rows.append(w)
            continue
        s = res.get(k + ("slice",))
        f = res.get(k + ("full",))
        pr = res.get(k + ("pristine",))
        w["slice"], w["slice_cpu"] = s
        if f:
            w["full"], w["full_cpu"] = f
            cmp_n += 1
            cpu_slice += s[1]
            cpu_full += f[1]
            if f[0] != s[0]:
                dis.append(w)
        if pr:
            w["pristine"] = pr[0]
        rows.append(w)
    nfull = sum(1 for w in work if w["mode"] == "full")
    print(f"\ncomparisons (slice vs full): {cmp_n}; full-fallback plans (no slice): {nfull}; "
          f"skipped heavy full: {sum(1 for w in work if w.get('skipped'))}")
    print(f"DISAGREEMENTS: {len(dis)}")
    for w in dis:
        print("  ", w)
    print(f"CPU slice {cpu_slice:.1f}s vs full {cpu_full:.1f}s ({100 * cpu_slice / max(cpu_full, 1e-9):.1f}%); wall {wall:.0f}s")
    fails = [w for w in rows if w.get("full") == "fail"]
    print(f"patch/test pairs where full test fails: {len(fails)}; slice also fails: "
          f"{sum(1 for w in fails if w['slice'] == 'fail')}")
    bad_pr = [w for w in rows if w.get("pristine") == "fail"]
    print(f"sample-fixture slices failing on pristine tree: {len(bad_pr)}")
    json.dump({"rows": rows, "disagreements": dis, "comparisons": cmp_n, "cpu_slice": cpu_slice,
               "cpu_full": cpu_full}, open(a.out, "w"), indent=1, default=str)


def tid_of_run(t, tid_of):
    return t[2]


if __name__ == "__main__":
    main()
