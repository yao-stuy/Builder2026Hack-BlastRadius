"""Run in the container: random single-line mutations of /src; sanity-check classify()."""
import difflib, glob, os, random, re, sys, collections
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br import classify as C, diffparse as dp

rnd = random.Random(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
N = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
files = glob.glob("/src/sqlglot/**/*.py", recursive=True)
SUBS = [(" + ", " - "), (" and ", " or "), (" == ", " != "), ("True", "False"), (" is not ", " is "), (" < ", " <= "), ("0", "1"), ("not ", "")]
stats = collections.Counter(); bad = 0
for _ in range(N):
    f = rnd.choice(files)
    text = open(f, encoding="utf-8", newline="").read()
    ls = text.split("\n")
    i = rnd.randrange(len(ls))
    mode = rnd.choice(["sub", "sub", "del", "dup", "ins"])
    line = ls[i]
    new = list(ls)
    if mode == "sub":
        c = [s for s in SUBS if s[0] in line]
        if not c: continue
        a, b = rnd.choice(c)
        new[i] = line.replace(a, b, 1)
    elif mode == "del":
        del new[i]
    elif mode == "dup":
        new.insert(i, line)
    else:
        ind = re.match(r"\s*", line).group()
        new.insert(i, ind + "_zz = 1")
    nt = "\n".join(new)
    if nt == text: continue
    d = "".join(difflib.unified_diff(text.splitlines(True), nt.splitlines(True), "a/x.py", "b/x.py", n=3))
    if not d: continue
    d = d.replace("\n\ No newline at end of file", "")  # files end with \n normally
    try:
        fp = dp.parse("diff --git a/x.py b/x.py\n" + d)[0]
        nt2 = dp.apply(fp, text)
    except dp.PatchError:
        continue
    im = C.classify("x.py", text, nt2, fp)
    stats[(mode, im.kind)] += 1
    if "internal error" in im.reason:
        bad += 1; print("INTERNAL", f, mode, i + 1, im.reason)
    if im.kind == "lines" and mode in ("sub", "del"):
        # the mutated old line must be covered unless it is blank/comment
        s = ls[i].strip()
        if s and not s.startswith("#") and (i + 1) not in im.old_lines:
            bad += 1; print("UNCOVERED", f, mode, i + 1, repr(ls[i]), sorted(im.old_lines)[:10])
    if im.kind == "noop" and mode == "sub":
        bad += 1; print("NOOP?", f, i + 1)
for k, v in sorted(stats.items()): print(k, v)
print("problems:", bad)
