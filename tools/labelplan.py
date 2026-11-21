"""Benchmark labeling helper (never used at run time).
For each patch in a bench set, list the slow tests that cannot be affected according to the coverage maps,
so the labeling run can skip them and finish sooner.
usage (lab container): BR_SQLGLOT=/src BR_MAPS=/repo/maps python tools/labelplan.py /scratch/bench/<set>"""
import glob, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import select

d = sys.argv[1]
tests = json.load(open("/kit/sample/tests.json"))["tests"]
slow = {t["number"]: t["id"] for t in tests if t["seconds"] >= 2.0}
ctx = select.Ctx(tests, lambda *a: None)
plan = {}
for f in sorted(glob.glob(d + "/commits/*.patch")):
    c = os.path.basename(f)[:-6]
    try:
        cand = select.analyze(ctx, open(f, "rb").read().decode("utf-8", errors="surrogateescape"))[0]
        plan[c] = [i for n, i in slow.items() if n not in cand]
    except Exception:
        plan[c] = []
json.dump(plan, open(d + "/plan.json", "w"))
import collections
print(len(plan), "patches; skipped-slow-count histogram:", sorted(collections.Counter(len(v) for v in plan.values()).items()))
