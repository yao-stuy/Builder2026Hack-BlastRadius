"""Run in the container: print classify() results for all sample patches."""
import glob, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br import classify, diffparse

def rng(s):
    s = sorted(s); out = []
    for x in s:
        if out and x == out[-1][1] + 1: out[-1][1] = x
        else: out.append([x, x])
    return ",".join(f"{a}" if a == b else f"{a}-{b}" for a, b in out)

print(f"{'commit':6} {'file':44} {'kind':11} {'lvl':5} {'shift':5} old_lines | scopes")
for p in sorted(glob.glob("/kit/sample/commits/*.patch")):
    c = os.path.basename(p)[:-6]
    for fp in diffparse.parse(open(p, encoding="utf-8", newline="").read()):
        old = open("/src/" + fp.old_path, encoding="utf-8", newline="").read()
        new = diffparse.apply(fp, old)
        im = classify.classify(fp.new_path, old, new, fp)
        ol = rng(im.old_lines)
        print(f"{c:6} {fp.new_path[-44:]:44} {im.kind:11} {str(im.level):5} {str(im.lineno_shift):5} {ol} | {','.join(im.scopes)} {im.reason if im.kind=='whole_file' else ''} {im.comment_text[:30] if im.kind=='noop' else ''}")
