"""Sanity checks for maps: load timings and recall on sample patches. Run: python tests_br/check_maps.py"""
import ast, json, os, re, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from br import maps

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "maps")
c0 = time.process_time(); maps.load(D); print(f"load index: {(time.process_time()-c0)*1000:.1f} ms cpu")
c0 = time.process_time(); maps.test_ids(); print(f"meta: {(time.process_time()-c0)*1000:.1f} ms")
for f in ("sqlglot/dialects/dialect.py", "sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/optimizer/qualify_columns.py"):
    c0 = time.process_time(); maps._cov(f); t = (time.process_time() - c0) * 1000
    c0 = time.process_time(); maps.tests_for_file(f); print(f"decode {f}: {t:.1f} ms, +union all lines {(time.process_time()-c0)*1000:.1f} ms")
tests = json.load(open("/kit/sample/tests.json"))["tests"]
ans = json.load(open("/kit/sample/answers.json"))["commits"]
assert maps.test_ids() == [t["id"] for t in tests]


def stmt_range(src, line):
    best = None
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.stmt) and n.lineno <= line <= n.end_lineno:
            if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.If, ast.For, ast.While, ast.With, ast.Try, ast.AsyncFunctionDef)):
                # header only
                body0 = n.body[0].lineno
                if not (n.lineno <= line < body0):
                    continue
                rng = (n.lineno, body0 - 1)
            else:
                rng = (n.lineno, n.end_lineno)
            if best is None or rng[1] - rng[0] <= best[1] - best[0]:
                best = rng
    return best


for pid in sys.argv[1:] or ["3utz", "2jrq", "ary6", "j6uz", "6ze8", "bj7g", "535z"]:
    patch = open(f"/kit/sample/commits/{pid}.patch").read()
    want = set(ans[pid]["failing"])
    cur = None; old = 0; changed = []
    for ln in patch.splitlines():
        if ln.startswith("+++ "):
            cur = ln[6:]
        elif ln.startswith("@@"):
            old = int(re.match(r"@@ -(\d+)", ln).group(1))
        elif ln.startswith("-") and not ln.startswith("---"):
            changed.append((cur, old)); old += 1
        elif ln.startswith("+") and not ln.startswith("+++"):
            pass
        elif ln.startswith(" "):
            old += 1
    cov = set(); rawcov = set(); nonpy = []
    for f, l in changed:
        if f.endswith(".py"):
            rawcov |= maps.tests_for(f, l)
            src = open("/src/" + f).read()
            r = stmt_range(src, l) or (l, l)
            for x in range(r[0], r[1] + 1):
                cov |= maps.tests_for(f, x)
        else:
            cov |= maps.tests_reading(f)
            rawcov |= maps.tests_reading(f)
            nonpy.append(f)
    print(f"{pid}: changed={changed} want={len(want)} raw_missing={sorted(want-rawcov)} stmt_missing={sorted(want-cov)} cand={len(cov)}")
