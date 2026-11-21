"""Record per-fixture-entry line coverage for tests that loop over fixture entries (build time).

usage:
  python tools/record_entries.py run   [--jobs 5] [--out /scratch/ent] [--only SUBSTR] [--skip SUBSTR ...]
                                       [--fresh] [--redo] [--src /src] [--root /tmp/s]
  python tools/record_entries.py build [--raw /scratch/ent] [--maps /repo/maps]

How it works (see br/slices.py for the loader filter):
  * candidate modules = tests/*.py that mention load_sql_fixture (automatic scan).
  * per module one TEMPLATE process (bare parent -> imports unittest, br.harness, br.slices, the test
    module, with sys.monitoring LINE recording on) forks one child per run.  After the fork the child
    calls sys.monitoring.restart_events() and clears the hit sets, so every line re-fires.  setUpClass,
    loaders, test body all run inside the child.  Lines executed by the template itself (import time of
    the module and whatever it pulled in) are unioned into `base`; they are executed in every fresh
    process too, so cov_i - base is identical to the fresh-process value.  --fresh disables the
    template import (module imported inside each child) to cross-check this.
  * per test: base run = all fixture files yield ZERO entries (this is also the discovery run: the
    wrapped loaders record every file iterated, entry counts, and whether it happened in setUpClass).
    Per-entry run i of file F = filter {F: {i}} with all other files empty.  Every run must pass.
  * files iterated only in setUpClass: one unfiltered probe run; if it adds no line over `base` the
    test does not use them ("unused", no per-entry runs); otherwise per-entry runs.
"""
import argparse
import collections
import glob
import json
import os
import pickle
import re
import select
import shutil
import signal
import struct
import sys
import time
import zlib

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CHILD_TIMEOUT = 900
PROBE_MAX_SECONDS = 3.0


# ---------------------------------------------------------------------------- recorder (as tools/record.py)

class Recorder:
    def __init__(self, root):
        import inspect
        self.mon = mon = sys.monitoring
        tool = None
        for i in range(6):
            if mon.get_tool(i) is None:
                tool = i
                break
        mon.use_tool_id(tool, "record_entries")
        self.tool = tool
        prefix = root.rstrip("/") + "/"
        self.top = top = {}
        self.fn = fn = {}
        cache = {}
        OPT = inspect.CO_OPTIMIZED
        DIS = mon.DISABLE

        def on_line(code, line):
            f = code.co_filename
            r = cache.get(f)
            if r is None:
                r = f[len(prefix):] if f.startswith(prefix) and f.endswith(".py") else False
                cache[f] = r
            if r:
                d = fn if code.co_flags & OPT else top
                s = d.get(r)
                if s is None:
                    s = d[r] = set()
                s.add(line)
            return DIS

        mon.register_callback(tool, mon.events.LINE, on_line)

    def start(self):
        self.mon.set_events(self.tool, self.mon.events.LINE)

    def stop(self):
        self.mon.set_events(self.tool, 0)

    def reset(self):
        self.fn.clear()
        self.top.clear()
        self.mon.restart_events()

    def snapshot(self):
        return {"fn": {k: set(v) for k, v in self.fn.items()},
                "top": {k: set(v) for k, v in self.top.items()}}


_NOSET = frozenset()


def _diff(cov, base):
    out = {}
    for kind in ("fn", "top"):
        d = {}
        b = base[kind]
        for f, s in cov[kind].items():
            r = s - b.get(f, _NOSET)
            if r:
                d[f] = sorted(r)
        out[kind] = d
    return out


def _union(a, b):
    out = {}
    for kind in ("fn", "top"):
        d = {f: set(s) for f, s in a[kind].items()}
        for f, s in b[kind].items():
            d.setdefault(f, set()).update(s)
        out[kind] = d
    return out


def _empty():
    return {"fn": {}, "top": {}}


# ---------------------------------------------------------------------------- forked job runner

def _class_phase_hook():
    import unittest
    from br import slices
    S = unittest.suite.TestSuite
    if getattr(S, "_br_phase_hooked", False):
        return
    orig = S._handleClassSetUp

    def w(self, test, result):
        slices.set_phase("class")
        try:
            return orig(self, test, result)
        finally:
            slices.set_phase("test")

    S._handleClassSetUp = w
    S._br_phase_hooked = True


def _job_child(wfd, rec, harness, slices, tid, filt, default_empty):
    code = 0
    try:
        os.setsid()
        dn = os.open(os.devnull, os.O_RDWR)
        os.dup2(dn, 0)
        os.dup2(dn, 1)
        if os.environ.get("RE_STDERR") != "1":
            os.dup2(dn, 2)
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGPIPE, signal.SIGALRM):
            try:
                signal.signal(s, signal.SIG_DFL)
            except Exception:
                pass
        rec.reset()
        rec.start()
        slices.reset_seen()
        slices.set_phase("test")
        slices.set_filter(filt, default_empty=default_empty)
        res = harness.run([tid])
        rec.stop()
        _, outcome, cpu = res[0]
        out = {"outcome": outcome, "cpu": cpu, "cov": rec.snapshot(), "seen": slices.seen()}
    except BaseException as e:  # noqa: BLE001
        out = {"outcome": "crash", "cpu": 0.0, "cov": _empty(), "seen": {}, "err": repr(e)}
        code = 3
    try:
        data = pickle.dumps(out, pickle.HIGHEST_PROTOCOL)
        mv = memoryview(struct.pack("<Q", len(data)) + data)
        while len(mv):
            n = os.write(wfd, mv)
            mv = mv[n:]
    except BaseException:
        code = 4
    os._exit(code)


def par_run(jobs, tasks, rec, harness, slices, on_done=None):
    """tasks: list of (key, tid, filt, default_empty). Runs forked children, <= jobs at a time.
    Returns {key: result}."""
    pending = list(tasks)
    running = {}  # rfd -> [pid, key, start, buf]
    results = {}
    while pending or running:
        while pending and len(running) < jobs:
            key, tid, filt, de = pending.pop(0)
            rfd, wfd = os.pipe()
            sys.stderr.flush()
            pid = os.fork()
            if pid == 0:
                os.close(rfd)
                _job_child(wfd, rec, harness, slices, tid, filt, de)
            os.close(wfd)
            running[rfd] = [pid, key, time.time(), bytearray()]
        if not running:
            continue
        r, _, _ = select.select(list(running), [], [], 1.0)
        for fd in list(running):
            pid, key, st, buf = running[fd]
            fin = False
            if fd in r:
                chunk = os.read(fd, 1 << 20)
                if chunk:
                    buf += chunk
                else:
                    fin = True
            if not fin and time.time() - st > CHILD_TIMEOUT:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except Exception:
                    pass
                results[key] = {"outcome": "timeout", "cpu": 0.0, "cov": _empty(), "seen": {}}
                fin = True
                buf = None
            if fin:
                os.close(fd)
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
                del running[fd]
                if buf is not None:
                    try:
                        n = struct.unpack("<Q", bytes(buf[:8]))[0]
                        results[key] = pickle.loads(bytes(buf[8:8 + n]))
                    except Exception:
                        results[key] = {"outcome": "crash", "cpu": 0.0, "cov": _empty(), "seen": {}}
                if on_done:
                    on_done(key, results[key])
    return results


# ---------------------------------------------------------------------------- per-module pipeline

def process_module(root, modname, tests, jobs, outdir, fresh, probe_max=PROBE_MAX_SECONDS, log=None):
    sys.path.insert(0, REPO)
    rec = Recorder(root)
    rec.start()
    from br import harness, slices
    harness.setup(root)
    slices.install()
    _class_phase_hook()
    if not fresh:
        import importlib
        importlib.import_module(modname)
    rec.stop()
    tcov = rec.snapshot()
    say = log or (lambda m: print(m, flush=True))

    # --- stage 1: base / discovery runs
    tasks = [(t["number"], t["id"], {}, True) for t in tests]
    base_res = par_run(jobs, tasks, rec, harness, slices)
    info = {}
    for t in tests:
        n = t["number"]
        r = base_res[n]
        base = _union(tcov, r["cov"])
        d = {"number": n, "id": t["id"], "base": {k: {f: sorted(s) for f, s in v.items()} for k, v in base.items()},
             "base_cpu": r["cpu"], "files": {}, "entries": {}}
        info[n] = (t, d, base)
        if r["outcome"] != "pass":
            d["notsliceable"] = f"fails with zero fixture entries ({r['outcome']}{' ' + r['err'] if r.get('err') else ''})"
            continue
        files = {}
        for (phase, fname, loader), (cnt, complete) in r["seen"].items():
            e = files.setdefault(fname, {"n": 0, "loader": loader, "phases": set(), "complete": True})
            if loader != e["loader"]:
                e["conflict"] = True
            e["n"] = max(e["n"], cnt)
            e["phases"].add(phase)
            e["complete"] = e["complete"] and complete
        for fname, e in list(files.items()):
            if e["n"] == 0 or not e["complete"] or e.get("conflict"):
                files.pop(fname)
        if not files:
            d["notsliceable"] = "never iterates a fixture loader to completion"
            continue
        d["files"] = {f: {"n": e["n"], "loader": e["loader"], "phase": "+".join(sorted(e["phases"])),
                          "unused": False} for f, e in files.items()}
    say(f"[{modname}] base/discovery done: {len(tests)} tests, "
        f"{sum(1 for v in info.values() if 'files' in v[1] and v[1]['files'])} with fixtures")

    # --- stage 2: probes for setUpClass-only files
    ptasks = []
    for n, (t, d, base) in info.items():
        if not d["files"] or "notsliceable" in d:
            continue
        if all(v["phase"] == "class" for v in d["files"].values()) and t["seconds"] < probe_max:
            ptasks.append((n, t["id"], {}, False))
    pres = par_run(jobs, ptasks, rec, harness, slices)
    for n, r in pres.items():
        t, d, base = info[n]
        if r["outcome"] != "pass":
            d["notsliceable"] = f"fails unfiltered in recorder ({r['outcome']})"
            continue
        extra = _diff(r["cov"], base)
        if not extra["fn"] and not extra["top"]:
            for v in d["files"].values():
                v["unused"] = True
        d["probe_cpu"] = r["cpu"]

    # --- stage 3: per-entry runs
    etasks = []
    for n, (t, d, base) in info.items():
        if not d["files"] or "notsliceable" in d:
            continue
        for fname, v in d["files"].items():
            if v["unused"]:
                continue
            for i in range(v["n"]):
                etasks.append(((n, fname, i), t["id"], {fname: {i}}, True))
    # heaviest tests first
    etasks.sort(key=lambda x: -(info[x[0][0]][0]["seconds"]))
    say(f"[{modname}] per-entry runs: {len(etasks)}")
    t0 = time.time()
    done = [0]
    store = {}  # (n, fname) -> {i: (diff, cpu)}
    failed = {}

    def on_done(key, r):
        n, fname, i = key
        done[0] += 1
        if r["outcome"] != "pass":
            failed.setdefault(n, f"entry {fname}#{i} fails at the pinned commit ({r['outcome']})")
            return
        base = info[n][2]
        diff = _diff(r["cov"], base)
        store.setdefault((n, fname), {})[i] = (diff, max(0.0, r["cpu"] - info[n][1]["base_cpu"]))
        if done[0] % 500 == 0:
            say(f"[{modname}] {done[0]}/{len(etasks)} elapsed={time.time() - t0:.0f}s")

    par_run(jobs, etasks, rec, harness, slices, on_done=on_done)
    nruns = len(ptasks) + len(tasks) + len(etasks)
    for n, (t, d, base) in info.items():
        if n in failed:
            d["notsliceable"] = failed[n]
        elif d["files"] and "notsliceable" not in d:
            for fname, v in d["files"].items():
                if v["unused"]:
                    continue
                ent = store.get((n, fname), {})
                if len(ent) != v["n"]:
                    d["notsliceable"] = f"missing entry runs for {fname}"
                    break
                d["entries"][fname] = [{"fn": ent[i][0]["fn"], "top": ent[i][0]["top"], "cpu": round(ent[i][1], 4)}
                                       for i in range(v["n"])]
        d["files"] = {k: v for k, v in d["files"].items()} if "notsliceable" not in d else {}
        d["runs"] = None
        tmp = os.path.join(outdir, f"{n}.json.tmp")
        with open(tmp, "w") as f:
            json.dump(d, f, separators=(",", ":"))
        os.rename(tmp, os.path.join(outdir, f"{n}.json"))
    say(f"[{modname}] done: {nruns} runs, wall {time.time() - t0:.0f}s (entry stage)")
    return nruns


# ---------------------------------------------------------------------------- driver

def find_modules(root, tests):
    mods = set()
    for p in glob.glob(os.path.join(root, "tests", "**", "*.py"), recursive=True):
        if p.endswith("helpers.py"):
            continue
        try:
            if "load_sql_fixture" in open(p, encoding="utf-8").read():
                mods.add(os.path.relpath(p, root).replace(os.sep, "/")[:-3])
        except OSError:
            pass
    return mods


def cmd_run(a):
    if not os.path.isdir(os.path.join(a.root, "sqlglot")):
        shutil.copytree(a.src, a.root, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    os.makedirs(a.out, exist_ok=True)
    tests = json.load(open(a.tests))["tests"]
    mods = find_modules(a.root, tests)
    by_mod = collections.defaultdict(list)
    for t in tests:
        m = t["id"].split("::")[0][:-3]
        if m in mods:
            if a.only and a.only not in t["id"]:
                continue
            if any(s in t["id"] for s in a.skip):
                continue
            if not a.redo and os.path.exists(os.path.join(a.out, f"{t['number']}.json")):
                continue
            by_mod[m].append(t)
    total = 0
    t0 = time.time()
    for m, ts in sorted(by_mod.items()):
        modname = m.replace("/", ".")
        print(f"== {modname}: {len(ts)} tests", flush=True)
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                process_module(a.root, modname, ts, a.jobs, a.out, a.fresh)
            except BaseException as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                code = 3
            os._exit(code)
        _, st = os.waitpid(pid, 0)
        print(f"== {modname}: exit status {st}", flush=True)
    print(f"all done, wall {time.time() - t0:.0f}s", flush=True)


def fullcpu_module(root, modname, rows, jobs, outdir, entries=False):
    sys.path.insert(0, REPO)
    rec = Recorder(root)  # not started: children measure plain CPU
    from br import harness, slices
    harness.setup(root)
    slices.install()
    import importlib
    importlib.import_module(modname)
    tasks = [(d["number"], d["id"], {}, False) for d in rows]
    res = par_run(jobs, tasks, rec, harness, slices)
    if entries:
        # unmonitored CPU of: zero-entry run and each single-entry run (monitoring inflates CPU)
        et = []
        for d in rows:
            et.append(((d["number"], None, None), d["id"], {}, True))
            for fname, ents in d["entries"].items():
                for i in range(len(ents)):
                    et.append(((d["number"], fname, i), d["id"], {fname: {i}}, True))
        er = par_run(jobs, et, rec, harness, slices)
        for d in rows:
            d["base_cpu_nm"] = er[(d["number"], None, None)]["cpu"]
            d["ecpu"] = {f: [er[(d["number"], f, i)]["cpu"] for i in range(len(e))] for f, e in d["entries"].items()}
    for d in rows:
        r = res[d["number"]]
        d["full_cpu"] = r["cpu"]
        d["full_outcome"] = r["outcome"]
        with open(os.path.join(outdir, f"{d['number']}.json"), "w") as f:
            json.dump(d, f, separators=(",", ":"))
        print(f"fullcpu {d['id']} {r['outcome']} {r['cpu']:.2f}s", flush=True)


def cmd_fullcpu(a):
    if not os.path.isdir(os.path.join(a.root, "sqlglot")):
        shutil.copytree("/src", a.root, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    by_mod = collections.defaultdict(list)
    for p in glob.glob(os.path.join(a.out, "*.json")):
        d = json.load(open(p))
        if "notsliceable" in d or not d.get("files"):
            continue
        if not a.redo and "full_cpu" in d:
            continue
        by_mod[d["id"].split("::")[0][:-3].replace("/", ".")].append(d)
    for modname, rows in sorted(by_mod.items()):
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                fullcpu_module(a.root, modname, rows, a.jobs, a.out, a.entries)
            except BaseException:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                code = 3
            os._exit(code)
        os.waitpid(pid, 0)


# ---------------------------------------------------------------------------- build compact map

def _dl(lines):
    out = []
    prev = 0
    for x in sorted(lines):
        out.append(x - prev)
        prev = x
    return out


def _cov_enc(cov):
    return {k: {f: _dl(v) for f, v in cov[k].items()} for k in ("fn", "top")}


def cmd_build(a):
    os.makedirs(a.maps, exist_ok=True)
    files = sorted(glob.glob(os.path.join(a.raw, "*.json")), key=lambda p: int(os.path.basename(p)[:-5]))
    idx = {"tests": {}, "nonsliceable": {}, "version": 1}
    out = open(os.path.join(a.maps, "entries.bin"), "wb")
    pos = 0
    for p in files:
        d = json.load(open(p))
        n = str(d["number"])
        if "notsliceable" in d or not d.get("files"):
            idx["nonsliceable"][n] = d.get("notsliceable", "no fixture files")
            continue
        blob = {"id": d["id"], "base": _cov_enc(d["base"]), "bcpu": round(d.get("base_cpu_nm", d["base_cpu"]), 4),
                "fcpu": round(d.get("full_cpu", 0.0), 4), "f": {}}
        for fname, v in d["files"].items():
            if not v["unused"] and not any(e["fn"] or e["top"] for e in d["entries"][fname]):
                v["unused"] = True  # no entry adds any line over base: the test does not depend on entries
            fe = {"n": v["n"], "l": v["loader"], "p": v["phase"], "u": v["unused"]}
            if not v["unused"]:
                ents = d["entries"][fname]
                fe["cpu"] = [round(max(0.0, x - d["base_cpu_nm"]), 4) for x in d["ecpu"][fname]] if "ecpu" in d                     else [e["cpu"] for e in ents]
                for kind in ("fn", "top"):
                    per = {}  # file -> {line: [entries]}
                    for i, e in enumerate(ents):
                        for f, lines in e[kind].items():
                            m = per.setdefault(f, {})
                            for ln in lines:
                                m.setdefault(ln, []).append(i)
                    enc = {}
                    for f, m in per.items():
                        ls = sorted(m)
                        # group identical entry lists
                        groups = {}
                        gl = []
                        gi = []
                        for ln in ls:
                            key = tuple(m[ln])
                            g = groups.get(key)
                            if g is None:
                                g = groups[key] = len(groups)
                                gl.append(_dl(key))
                            gi.append(g)
                        enc[f] = {"L": _dl(ls), "G": gi, "S": gl}
                    fe[kind] = enc
            blob["f"][fname] = fe
        raw = zlib.compress(json.dumps(blob, separators=(",", ":")).encode(), 9)
        out.write(raw)
        idx["tests"][n] = [pos, len(raw)]
        pos += len(raw)
    out.close()
    with open(os.path.join(a.maps, "entries.idx.json"), "w") as f:
        json.dump(idx, f, separators=(",", ":"), sort_keys=True)
    print(f"entries.bin {pos} bytes, sliceable tests {len(idx['tests'])}, not sliceable {len(idx['nonsliceable'])}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--jobs", type=int, default=5)
    r.add_argument("--out", default="/scratch/ent")
    r.add_argument("--only", default=None)
    r.add_argument("--skip", action="append", default=[])
    r.add_argument("--fresh", action="store_true")
    r.add_argument("--redo", action="store_true")
    r.add_argument("--tests", default="/kit/sample/tests.json")
    r.add_argument("--src", default="/src")
    r.add_argument("--root", default="/tmp/s")
    f = sub.add_parser("fullcpu")
    f.add_argument("--jobs", type=int, default=3)
    f.add_argument("--out", default="/scratch/ent")
    f.add_argument("--root", default="/tmp/s")
    f.add_argument("--redo", action="store_true")
    f.add_argument("--entries", action="store_true", help="also unmonitored per-entry CPU")
    b = sub.add_parser("build")
    b.add_argument("--raw", default="/scratch/ent")
    b.add_argument("--maps", default=os.path.join(REPO, "maps"))
    a = ap.parse_args()
    {"run": cmd_run, "build": cmd_build, "fullcpu": cmd_fullcpu}[a.cmd](a)


if __name__ == "__main__":
    main()
