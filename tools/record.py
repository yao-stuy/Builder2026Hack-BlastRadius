"""Record per-test line coverage (sys.monitoring) at the pinned commit, one forked child per test.

usage: python tools/record.py [--jobs 4] [--only SUBSTR] [--limit N] [--out /scratch/rec]
                              [--tests /kit/sample/tests.json] [--src /src] [--root /tmp/s]
The parent imports only stdlib + duckdb/pandas/numpy; nothing from sqlglot/tests (those are imported in
each child after monitoring is enabled).
"""
import argparse
import json
import os
import shutil
import signal
import sys
import time

# NOTE: do not preload duckdb/pandas here: meta records whether each test imports them.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CHILD_TIMEOUT = 600
HEAVY_SECONDS = 3.0


def child(root, test, outdir):
    import builtins
    import inspect
    import io
    import gzip

    mon = sys.monitoring
    tool = None
    for i in range(6):
        if mon.get_tool(i) is None:
            tool = i
            break
    mon.use_tool_id(tool, "record")
    prefix = root.rstrip("/") + "/"
    top = {}
    fn = {}
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
    mon.set_events(tool, mon.events.LINE)

    import classhook
    classhook.install(root)

    reads = set()

    def note(path, isdir=False):
        try:
            if isinstance(path, int):
                return
            p = os.path.abspath(os.fspath(path))
            if isinstance(p, bytes):
                p = p.decode()
        except Exception:
            return
        if p.startswith(prefix) or p == prefix[:-1]:
            rel = p[len(prefix):] if p != prefix[:-1] else "."
            if "__pycache__" in rel or rel.endswith(".pyc"):
                return
            reads.add(("dir:" + rel) if isdir else rel)

    def wrap_open(orig):
        def _open(file, *a, **k):
            note(file)
            return orig(file, *a, **k)
        return _open

    builtins.open = wrap_open(builtins.open)
    io.open = wrap_open(io.open)
    gzip.open = wrap_open(gzip.open)
    o_ld, o_sd = os.listdir, os.scandir

    def listdir(path="."):
        note(path, True)
        return o_ld(path)

    def scandir(path="."):
        note(path, True)
        return o_sd(path)

    os.listdir, os.scandir = listdir, scandir

    from br import harness  # stdlib-only; imports nothing from root
    harness.setup(root)
    res = harness.run([test["id"]], pool_mode="inline")
    mon.set_events(tool, 0)
    _, outcome, cpu = res[0]
    n = test["number"]
    data = {
        "number": n, "id": test["id"], "outcome": outcome, "cpu": round(cpu, 4),
        "fn": {k: sorted(v) for k, v in fn.items()},
        "top": {k: sorted(v) for k, v in top.items()},
        "reads": sorted(reads),
        "uses": classhook.used_keys(),
        "mods": [("duckdb" in sys.modules), ("pandas" in sys.modules)],
    }
    tmp = os.path.join(outdir, f"{n}.json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.rename(tmp, os.path.join(outdir, f"{n}.json"))


def run_child(root, test, outdir):
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.setsid()
            sys.path.insert(0, REPO)
            sys.path.insert(0, HERE)
            child(root, test, outdir)
        except BaseException as e:  # noqa: BLE001
            sys.stderr.write(f"child {test['number']} error: {e!r}\n")
            code = 3
        finally:
            try:
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(code)
    return pid


def ok_done(outdir, n):
    p = os.path.join(outdir, f"{n}.json")
    if not os.path.exists(p):
        return False
    try:
        with open(p) as f:
            return "error" not in json.load(f)
    except Exception:
        return False


def write_error(outdir, test, why):
    n = test["number"]
    if ok_done(outdir, n):
        return
    with open(os.path.join(outdir, f"{n}.json"), "w") as f:
        json.dump({"number": n, "id": test["id"], "error": why}, f)


def run_batch(tests, jobs, root, outdir):
    pending = list(tests)
    running = {}  # pid -> (test, start)
    nfail = 0
    done = 0
    total = len(pending)
    t_start = time.time()
    while pending or running:
        while pending and len(running) < jobs:
            t = pending.pop(0)
            running[run_child(root, t, outdir)] = (t, time.time())
        time.sleep(0.02)
        for pid in list(running):
            t, st = running[pid]
            try:
                wp, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                wp, status = pid, 255 << 8
            if wp == 0:
                if time.time() - st > CHILD_TIMEOUT:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except Exception:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except Exception:
                            pass
                    os.waitpid(pid, 0)
                    write_error(outdir, t, "timeout")
                    nfail += 1
                    done += 1
                    del running[pid]
                continue
            del running[pid]
            done += 1
            if not ok_done(outdir, t["number"]):
                write_error(outdir, t, f"crash status={status}")
                nfail += 1
            if done % 50 == 0:
                print(f"[{done}/{total}] fail_to_record={nfail} elapsed={time.time()-t_start:.0f}s", flush=True)
    return nfail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--only", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="/scratch/rec")
    ap.add_argument("--tests", default="/kit/sample/tests.json")
    ap.add_argument("--src", default="/src")
    ap.add_argument("--root", default="/tmp/s")
    ap.add_argument("--redo", action="store_true")
    a = ap.parse_args()
    if not os.path.isdir(os.path.join(a.root, "sqlglot")):
        shutil.copytree(a.src, a.root, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    os.makedirs(a.out, exist_ok=True)
    tests = json.load(open(a.tests))["tests"]
    sel = [t for t in tests if (a.only is None or a.only in t["id"])]
    if a.limit:
        sel = sel[: a.limit]
    if not a.redo:
        sel = [t for t in sel if not ok_done(a.out, t["number"])]
    heavy = [t for t in sel if t["seconds"] >= HEAVY_SECONDS]
    rest = [t for t in sel if t["seconds"] < HEAVY_SECONDS]
    print(f"to record: {len(sel)} (heavy serial: {len(heavy)})", flush=True)
    nf = run_batch(heavy, 1, a.root, a.out)
    nf += run_batch(rest, a.jobs, a.root, a.out)
    print(f"done; failed to record: {nf}", flush=True)


if __name__ == "__main__":
    main()
