import json, re, sys
sys.path.insert(0, "/repo")
from br import maps
maps.load("/repo/maps")
meta = json.load(open("/scratch/bench/mn/meta.json"))
want = ("sqlglot/parser.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py", "sqlglot/expressions/core.py")
out = []
for k, v in sorted(meta.items()):
    if not any(f in want for f in v["files"]): continue
    p = open(f"/scratch/bench/mn/commits/{k}.patch").read()
    f = None; hits = False; ch = []
    for line in p.splitlines():
        if line.startswith("--- a/"): f = line[6:]
        elif line.startswith("@@"):
            old = int(re.match(r"@@ -(\d+)", line).group(1)); cur = old
        elif line.startswith("-") and not line.startswith("---"):
            ch.append((f, cur)); cur += 1
        elif line.startswith("+") and not line.startswith("+++"):
            ch.append((f, cur))
        elif line.startswith(" "): cur += 1
    hit = any(1490 in maps.tests_for(f, l) for f, l in ch if f in want)
    out.append((k, hit, v["kind"], v["description"][:90]))
for o in out: print(o)
print(sum(1 for o in out if o[1]), "hit of", len(out))
