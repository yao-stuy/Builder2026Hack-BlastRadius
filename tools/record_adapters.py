"""Build maps/adapters.json (source hashes) and maps/adapters.json.z (per-dialect coverage) for br/adapters.py.

usage (inside the lab container):
  python tools/record_adapters.py [--src /src] [--maps /repo/maps] [--only DIALECT ...]

generator_memory: coverage of running ONLY dialect D of test_generator_memory in a fresh forked child
(template process imports tests.test_generator with sys.monitoring on; the child resets the recorder and
runs adapters.generator_memory_run([D])).  base = template import lines + the zero-dialect run.
Stored per dialect: lines not in base (fn/top, delta encoded).
"""
import argparse
import json
import os
import pickle
import struct
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")


def _delta(lines):
    out = []
    prev = 0
    for i, x in enumerate(sorted(lines)):
        out.append(x if i == 0 else x - prev)
        prev = x
    return out


def _child(wfd, rec, adapters, names):
    code = 0
    try:
        os.setsid()
        dn = os.open(os.devnull, os.O_RDWR)
        os.dup2(dn, 0)
        os.dup2(dn, 1)
        rec.reset()
        rec.start()
        t0 = time.process_time()
        res = adapters.generator_memory_run(names)
        cpu = time.process_time() - t0
        rec.stop()
        out = {"res": res, "cpu": cpu, "cov": rec.snapshot()}
    except BaseException as e:  # noqa: BLE001
        out = {"res": "crash:" + repr(e), "cpu": 0.0, "cov": {"fn": {}, "top": {}}}
        code = 3
    data = pickle.dumps(out, pickle.HIGHEST_PROTOCOL)
    mv = memoryview(struct.pack("<Q", len(data)) + data)
    while len(mv):
        mv = mv[os.write(wfd, mv):]
    os._exit(code)


def fork_run(rec, adapters, names):
    rfd, wfd = os.pipe()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        _child(wfd, rec, adapters, names)
    os.close(wfd)
    buf = bytearray()
    while True:
        c = os.read(rfd, 1 << 20)
        if not c:
            break
        buf += c
    os.close(rfd)
    os.waitpid(pid, 0)
    n = struct.unpack("<Q", bytes(buf[:8]))[0]
    return pickle.loads(bytes(buf[8:8 + n]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/src")
    ap.add_argument("--maps", default=os.path.join(REPO, "maps"))
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()

    from record_entries import Recorder
    rec = Recorder(a.src)
    rec.start()
    from br import harness, adapters
    harness.setup(a.src)
    import importlib
    tex = importlib.import_module("tests.test_executor")
    tpa = importlib.import_module("tests.test_parser")
    tge = importlib.import_module("tests.test_generator")
    rec.stop()
    tcov = rec.snapshot()

    hashes = {
        "executor_setUpClass": adapters.source_hash(tex.TestExecutor.__dict__["setUpClass"].__func__),
        "test_max_nodes": adapters.source_hash(tpa.TestParser.test_max_nodes),
        "test_generator_memory": adapters.source_hash(tge.TestGenerator.test_generator_memory),
    }
    adapters._hashes = hashes
    os.makedirs(a.maps, exist_ok=True)
    with open(os.path.join(a.maps, "adapters.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"hashes": hashes}, fh, indent=1, sort_keys=True)
        fh.write("\n")

    # ---- generator memory
    from sqlglot.dialects import Dialects
    names = [m.name for m in Dialects]
    if a.only:
        names = [n for n in names if n in a.only]
    zero = fork_run(rec, adapters, [])
    assert zero["res"] == "pass", zero["res"]
    base = {k: {} for k in ("fn", "top")}
    for src in (tcov, zero["cov"]):
        for k in ("fn", "top"):
            for f, s in src[k].items():
                base[k].setdefault(f, set()).update(s)
    per = {}
    costs = {}
    for n in names:
        r = fork_run(rec, adapters, [n])
        costs[n] = round(r["cpu"], 3)
        print(f"{n:12s} {r['res']} cpu={r['cpu']:.3f}", file=sys.stderr, flush=True)
        if r["res"] != "pass":
            print("WARNING: dialect did not pass alone:", n, r["res"], file=sys.stderr)
        cov = {}
        for k in ("fn", "top"):
            cov[k] = {}
            for f, s in r["cov"][k].items():
                d = s - base[k].get(f, set())
                if d:
                    cov[k][f] = _delta(d)
        per[n] = cov
    out = {
        "generator_memory": {
            "base": {k: {f: _delta(s) for f, s in base[k].items()} for k in ("fn", "top")},
            "dialects": per,
            "cpu": costs,
        }
    }
    raw = json.dumps(out, separators=(",", ":")).encode()
    with open(os.path.join(a.maps, "adapters.json.z"), "wb") as fh:
        fh.write(zlib.compress(raw, 9))
    print(f"wrote adapters.json(.z): {len(zlib.compress(raw, 9))} bytes compressed", file=sys.stderr)


if __name__ == "__main__":
    main()
