"""Validation of br.cmptypes inside the lab container.

  python tests_br/validate_cmptypes.py samples            # organizer samples (2haz, 66ks proved; others not)
  python tests_br/validate_cmptypes.py labeled            # every labeled python-only patch in /scratch/ready_*
  python tests_br/validate_cmptypes.py gen [--n 150]      # write swap patches to /scratch/bench/iseq/commits
  python tests_br/validate_cmptypes.py truth [--proved 40 --unproved 20]   # ground truth on generated patches
"""
import argparse
import ast
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from br import cmptypes, diffparse, maps  # noqa: E402

SRC = "/src"
MAPS = os.environ.get("BR_MAPS", "/repo/maps")
HEAVY = ("test_max_nodes", "test_execute_tpcds")
LABELED = ["/scratch/ready_trial_sample", "/scratch/ready_top1", "/scratch/ready_trial_novel", "/scratch/ready_all"]


def verdict(patch_text, src=SRC):
    """-> (all-python-only?, proved?, [explain])"""
    fps = diffparse.parse(patch_text)
    if not fps:
        return False, False, ["empty"]
    why = []
    proved = True
    for fp in fps:
        o, n = fp.old_path, fp.new_path
        if not (o and n and o == n and o.endswith(".py")):
            return False, False, ["non-python or rename/add/delete: %s" % (o or n)]
        old = open(os.path.join(src, o)).read()
        new = diffparse.apply(fp, old)
        ok = cmptypes.provably_equivalent(o, old, new)
        why.append(cmptypes.explain(o, old, new))
        proved = proved and ok
    return True, proved, why


def cmd_samples(a):
    maps.load(MAPS)
    truth = json.load(open("/kit/sample/answers.json"))["commits"]
    bad = 0
    for cid in sorted(truth):
        text = open("/kit/sample/commits/%s.patch" % cid).read()
        try:
            _, proved, why = verdict(text)
        except Exception as e:  # noqa: BLE001
            proved, why = False, ["error %r" % e]
        want = cid in ("2haz", "66ks")
        flag = "OK " if proved == want else "BAD"
        bad += proved != want
        if proved or want:
            print(flag, cid, proved, truth[cid]["failing"][:3], why)
    print("sample mismatches:", bad)


def cmd_labeled(a):
    maps.load(MAPS)
    tot = proved_n = 0
    viol = []
    kinds = {}
    seen = set()
    for d in LABELED:
        try:
            truth = json.load(open(d + "/answers.json"))["commits"]
        except Exception as e:  # noqa: BLE001
            print("skip", d, e)
            continue
        for cid, t in sorted(truth.items()):
            pf = "%s/commits/%s.patch" % (d, cid)
            if not os.path.exists(pf):
                continue
            text = open(pf).read()
            if text in seen:
                continue
            seen.add(text)
            tot += 1
            try:
                pyonly, proved, why = verdict(text)
            except Exception as e:  # noqa: BLE001
                continue
            if pyonly and proved:
                proved_n += 1
                kinds[t.get("kind")] = kinds.get(t.get("kind"), 0) + 1
                print("proved", d, cid, t.get("kind"), "failing=%d" % len(t["failing"]), why)
                if t["failing"]:
                    viol.append((d, cid, t["failing"], why))
    print("distinct patches %d, proved %d, kinds %s" % (tot, proved_n, kinds))
    print("VIOLATIONS:", len(viol))
    for v in viol:
        print("  ", v)


# ----------------------------------------------------------------------------- generation
OPRE = re.compile(r"\bis\s+not\b|\bis\b|==|!=")
SWAP = {"is": "==", "is not": "!=", "==": "is", "!=": "is not"}


def sites_in(path):
    src = open(os.path.join(SRC, path), "rb").read()
    lines = src.split(b"\n")
    tree = ast.parse(src)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
            r = n.comparators[0]
            if n.left.end_lineno != r.lineno:
                if n.left.end_lineno == r.lineno:
                    pass
            # operator lies between end of left and start of right
            if n.left.end_lineno == r.lineno:
                seg = lines[r.lineno - 1][n.left.end_col_offset:r.col_offset]
                ln = r.lineno
                base = n.left.end_col_offset
            else:
                # operator on the line where left ends (after it) or on the right's line (before it)
                seg = lines[n.left.end_lineno - 1][n.left.end_col_offset:]
                ln = n.left.end_lineno
                base = n.left.end_col_offset
                if not OPRE.search(seg.decode()):
                    seg = lines[r.lineno - 1][:r.col_offset]
                    ln = r.lineno
                    base = 0
            s = seg.decode()
            if "#" in s:
                continue
            ms = list(OPRE.finditer(s))
            if len(ms) != 1:
                continue
            m = ms[0]
            out.append((n.lineno, ln, base + len(s[:m.start()].encode()), base + len(s[:m.end()].encode()),
                        re.sub(r"\s+", " ", m.group())))
    return out


def cmd_gen(a):
    rnd = random.Random(a.seed)
    files = []
    for dp, _, fs in os.walk(os.path.join(SRC, "sqlglot")):
        for f in fs:
            if f.endswith(".py"):
                files.append(os.path.relpath(os.path.join(dp, f), SRC))
    files.sort()
    allsites = []
    for p in files:
        for s in sites_in(p):
            allsites.append((p, s))
    print("candidate sites:", len(allsites))
    rnd.shuffle(allsites)
    out = "/scratch/bench/iseq"
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out + "/commits")
    work = "/tmp/gitw"
    shutil.copytree(SRC, work, ignore=shutil.ignore_patterns(".git", "__pycache__", "sqlglot-integration-tests"))
    g = lambda *args: subprocess.run(["git", "-c", "user.email=x@x", "-c", "user.name=x", "-C", work, *args],
                                     capture_output=True, text=True, check=True).stdout
    g("init", "-q")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    ids = set()
    meta = {}
    for p, (ln0, ln, c0, c1, op) in allsites:
        if len(ids) >= a.n:
            break
        fn = os.path.join(work, p)
        lines = open(fn, "rb").read().split(b"\n")
        L = lines[ln - 1]
        lines[ln - 1] = L[:c0] + SWAP[op].encode() + L[c1:]
        open(fn, "wb").write(b"\n".join(lines))
        diff = g("diff")
        g("checkout", "--", p)
        if not diff:
            continue
        while True:
            cid = "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(4))
            if cid not in ids:
                break
        ids.add(cid)
        open("%s/commits/%s.patch" % (out, cid), "w").write(diff)
        meta[cid] = {"path": p, "line": ln0, "op": op}
    json.dump(meta, open(out + "/meta.json", "w"))
    print("wrote", len(ids), "patches to", out)


def cmd_truth(a):
    maps.load(MAPS)
    meta = json.load(open("/scratch/bench/iseq/meta.json"))
    rnd = random.Random(7)
    res = {}
    for cid in sorted(meta):
        text = open("/scratch/bench/iseq/commits/%s.patch" % cid).read()
        _, proved, why = verdict(text)
        res[cid] = (proved, why)
    P = [c for c in sorted(res) if res[c][0]]
    U = [c for c in sorted(res) if not res[c][0]]
    print("generated %d: proved %d, not proved %d" % (len(res), len(P), len(U)))
    for c in U[:60]:
        print("  unproved", c, meta[c]["path"], meta[c]["line"], res[c][1][0][:150])
    rnd.shuffle(P)
    rnd.shuffle(U)
    ids = maps.test_ids()
    from br import harness, overlay
    mir = overlay.Mirror(SRC, "/tmp/mirror")
    mir.build()
    summary = {"proved": [], "unproved": []}
    ntest = {}
    for group, cids in (("proved", P[:a.proved]), ("unproved", U[:a.unproved])):
        for cid in cids:
            m = meta[cid]
            cov = set()
            text = open("/scratch/bench/iseq/commits/%s.patch" % cid).read()
            fp = diffparse.parse(text)[0]
            for ln in range(m["line"], m["line"] + 6):  # multi-line compare: check a few following lines too
                pass
            old = open(os.path.join(SRC, m["path"])).read()
            new = diffparse.apply(fp, old)
            # covering tests: every line of the changed compare (old/new differ on 1 line; take the diff lines)
            changed = [i + 1 for i, (x, y) in enumerate(zip(old.split("\n"), new.split("\n"))) if x != y]
            for ln in changed + [m["line"]]:
                cov |= set(maps.tests_for(m["path"], ln, "any"))
            tl = [ids[n] for n in sorted(cov) if not any(h in ids[n] for h in HEAVY)]
            rnd.shuffle(tl)
            tl = tl[:60]
            mir.apply({m["path"]: new.encode()})
            failed = run_child(mir.dst, tl)
            mir.restore()
            summary[group].append((cid, m["path"], m["line"], m["op"], len(cov), len(tl), failed))
            print(group, cid, m["path"], m["line"], m["op"], "covering=%d ran=%d failed=%s" % (len(cov), len(tl), failed), flush=True)
    bad_p = [x for x in summary["proved"] if x[6]]
    brk_u = [x for x in summary["unproved"] if x[6]]
    print("PROVED with failing covering test: %d of %d  %s" % (len(bad_p), len(summary["proved"]), bad_p))
    print("NOT proved: %d of %d break a covering test" % (len(brk_u), len(summary["unproved"])))
    json.dump(summary, open("/scratch/bench/iseq/truth.json", "w"))


def run_child(root, tl):
    if not tl:
        return []
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(r)
            from br import harness
            harness.setup(root)
            res = harness.run(tl, pool_mode="inline")
            os.write(w, json.dumps([x[0] for x in res if x[1] != "pass"]).encode())
        except BaseException as e:  # noqa: BLE001
            os.write(w, json.dumps(["CHILD-ERROR %r" % e]).encode())
            code = 3
        finally:
            os._exit(code)
    os.close(w)
    buf = b""
    while True:
        b = os.read(r, 65536)
        if not b:
            break
        buf += b
    os.waitpid(pid, 0)
    try:
        return json.loads(buf)
    except Exception:
        return ["NO-RESULT"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["samples", "labeled", "gen", "truth"])
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--proved", type=int, default=40)
    ap.add_argument("--unproved", type=int, default=20)
    a = ap.parse_args()
    {"samples": cmd_samples, "labeled": cmd_labeled, "gen": cmd_gen, "truth": cmd_truth}[a.cmd](a)


if __name__ == "__main__":
    main()
