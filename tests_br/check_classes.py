"""Class-usage sanity + recall check.
usage: python tests_br/check_classes.py [sample_dir ...]   (default: /kit/sample + /scratch/ready_trial_*)"""
import json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from br import maps, diffparse, classify

maps.load(os.path.join(HERE, "..", "maps"))
t0 = time.process_time(); maps._load_cls(); print(f"class table load {(time.process_time()-t0)*1000:.1f} ms")
t0 = time.process_time(); n = len(maps.tests_using_class("sqlglot/dialects/tsql.py::TSQL")); print(f"tests_using_class(TSQL)={n} {(time.process_time()-t0)*1000:.1f} ms")
ours = {tid: i for i, tid in enumerate(maps.test_ids())}

for key, f, ln in [("TSQL", "sqlglot/dialects/tsql.py", 17), ("TriggerProperties", "sqlglot/expressions/ddl.py", 82),
                   ("snowflake", "sqlglot/dialects/snowflake.py", 133), ("singlestore", "sqlglot/parsers/singlestore.py", 213)]:
    c = maps.class_at(f, ln)
    print(f"{f}:{ln} class={c} tracked={maps.class_tracked(c) if c else None} "
          f"n_use={len(maps.tests_using_class(c)) if c else None} family={sorted(maps.class_family(c)) if c else None}")

dirs = sys.argv[1:] or ["/kit/sample", "/scratch/ready_trial_sample", "/scratch/ready_trial_novel"]
tot = viol = checked = 0
for d in dirs:
    if not os.path.isdir(d):
        continue
    tj = json.load(open(os.path.join(d, "tests.json")))["tests"]
    num2id = {t["number"]: t["id"] for t in tj}
    ans = json.load(open(os.path.join(d, "answers.json")))["commits"]
    for pid in sorted(ans):
        pf = os.path.join(d, "commits", pid + ".patch")
        if not os.path.exists(pf):
            continue
        tot += 1
        try:
            fps = diffparse.parse(open(pf).read())
        except Exception as e:
            print(d, pid, "parse error", e); continue
        cand = set(); ok = False; bad = False
        for fp in fps:
            path = fp.old_path if hasattr(fp, "old_path") else fp.path
            if not path or not path.startswith("sqlglot/") or not path.endswith(".py"):
                continue
            old = open("/src/" + path).read()
            new = diffparse.apply(fp, old)
            imp = classify.classify(path, old, new, fp)
            if imp.kind != "lines" or imp.level != "top":
                continue
            classes = {maps.class_at(path, l) for l in imp.old_lines}
            if None in classes or not classes:
                continue
            ok = True
            for c in classes:
                cand |= maps.tests_using_class(c)
                s, e = maps.class_span(c)
                for l in maps.lines(path):
                    if s <= l <= e:
                        cand |= maps.tests_for(path, l, "fn")
        if not ok:
            continue
        checked += 1
        fail = {ours[num2id[x]] for x in ans[pid]["failing"] if num2id.get(x) in ours}
        miss = sorted(fail - cand)
        print(f"{os.path.basename(d)}/{pid}: class-level, cand={len(cand)} failing={len(fail)} missing={miss}")
        if miss:
            viol += 1
            print("   VIOLATION ids:", [maps.test_ids()[m] for m in miss])
print(f"patches={tot} class-level checked={checked} violations={viol}")
