"""Entry point. Reads /data, prints one JSON line per commit on stdout. Logs go to stderr.

Safety net: whatever goes wrong, every commit still gets a valid line (worst case the full suite).
"""
import json
import os
import sys
import time

DATA = os.environ.get("BR_DATA", "/data")
T0 = time.time()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def _lpt(tests, k):
    loads = [0] * k
    workers = [[] for _ in range(k)]
    for n, ms in sorted(tests, key=lambda t: -t[1]):
        i = loads.index(min(loads))
        workers[i].append(n)
        loads[i] += ms
    return [w for w in workers if w], max(loads)


def pack(tests):
    """tests: list of (number, ms). Returns worker lists minimizing makespan + 2 s per worker."""
    if not tests:
        return []
    try:
        from br import pack as _p
        return _p.best(tests)
    except Exception:
        best = None
        for k in range(1, 9):
            w, mk = _lpt(tests, k)
            c = mk + 2000 * len(w)
            if best is None or c < best[0]:
                best = (c, w)
        return best[1]


def main():
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    cdir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))
    if os.environ.get("BR_REVERSE") == "1":   # test aid: answers must not depend on processing order
        commits.reverse()
    full = pack(sorted(ms.items()))

    answers = {}
    try:
        from br import select  # the real selector; absent in the bootstrap version
        # The whole batch has 3 minutes (4.5 in the final, which has about 1.5x as many commits).
        budget = float(os.environ.get("BR_BUDGET_S", "0")) or (140.0 if len(commits) <= 46 else 215.0)
        answers = select.run(DATA, tests, commits, deadline=T0 + budget, log=log)
    except Exception as e:  # noqa: BLE001
        log("selector unavailable or failed, using full suite:", repr(e))

    out = []
    for c in commits:
        w = answers.get(c)
        if w is None:
            w = full
        out.append(json.dumps({"commit": c, "workers": w}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    log(f"done: {len(commits)} commits in {time.time() - T0:.2f}s wall, {time.process_time():.2f}s cpu (parent)")


if __name__ == "__main__":
    main()
