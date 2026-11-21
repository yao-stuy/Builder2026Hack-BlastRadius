"""Offline safety check: static candidates must contain every truly failing test.
usage (in lab container): BR_SQLGLOT=/src BR_MAPS=/repo/maps python tools/recall.py <sample-shaped dir> [--show]"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import select

d = sys.argv[1]
tests = json.load(open(d + "/tests.json"))["tests"]
ans = json.load(open(d + "/answers.json"))["commits"]
ctx = select.Ctx(tests, lambda *a: None)
miss = 0; full = 0; tot = 0; sizes = []
for c in sorted(ans):
    truth = set(ans[c]["failing"]); tot += 1
    try:
        text = open(f"{d}/commits/{c}.patch", "rb").read().decode("utf-8", errors="surrogateescape")
        cand, certain, changes, notes, _ = select.analyze(ctx, text)
    except select.Full as e:
        full += 1
        if "--show" in sys.argv: print(c, ans[c]["kind"], "FULL", e)
        continue
    sizes.append(len(cand))
    lost = truth - cand
    if lost:
        miss += 1
        print("MISS", c, ans[c]["kind"], "truth", len(truth), "cand", len(cand), "lost", [tests[i]["id"] for i in sorted(lost)][:5], "|", "; ".join(notes)[:300])
    elif "--show" in sys.argv:
        print(c, ans[c]["kind"], "truth", len(truth), "cand", len(cand), "certain" if certain else "", "|", "; ".join(notes)[:160])
print(f"commits {tot}  candidate-misses {miss}  full-suite {full}  median cand {sorted(sizes)[len(sizes)//2] if sizes else 0}")
