"""End-to-end: overlay + engine + real harness on sample commits (run in lab container).
usage: python tests_br/test_engine_e2e.py [commit ids...]"""
import compileall, json, os, re, shutil, subprocess, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br.overlay import Mirror
from br.engine import Verifier

BAD = ("test_max_nodes", "test_execute_tpcds")


def main(ids):
    tests = json.load(open("/kit/sample/tests.json"))["tests"]
    ans = json.load(open("/kit/sample/answers.json"))["commits"]
    byn = {t["number"]: t for t in tests}
    for d in ("/tmp/s", "/tmp/m", "/tmp/pp"):
        shutil.rmtree(d, ignore_errors=True)
    shutil.copytree("/src", "/tmp/s", ignore=shutil.ignore_patterns(".git"))
    compileall.compile_dir("/tmp/s/sqlglot", quiet=2)
    compileall.compile_dir("/tmp/s/tests", quiet=2)
    mir = Mirror("/tmp/s", "/tmp/m"); mir.build()
    ver = Verifier("/tmp/m", preload=["unittest", "duckdb", "pandas", "numpy"])
    if os.environ.get("WARM"):
        ver.warm(["sqlglot", "tests.dialects.test_dialect"])
        print("warm modules", len(ver.warm_mods), file=sys.stderr)
    ok_all = True
    for cid in ids:
        patch = "/kit/sample/commits/%s.patch" % cid
        shutil.rmtree("/tmp/pp", ignore_errors=True)
        shutil.copytree("/src", "/tmp/pp", ignore=shutil.ignore_patterns(".git"))
        subprocess.run(["git", "apply", patch], cwd="/tmp/pp", check=True)
        files = re.findall(r"^\+\+\+ b/(.+)$", open(patch).read(), re.M)
        changes = {f: open("/tmp/pp/" + f, "rb").read() for f in files}
        fail = [byn[n] for n in ans[cid]["failing"]]
        files_t = {t["file"] for t in fail}
        others = [t for t in tests if t["file"] in files_t and t not in fail and not any(b in t["id"] for b in BAD)][:10]
        if not others:
            others = [t for t in tests if t["file"] == "tests/test_transpile.py"][:10]
        req = [t["id"] for t in fail + others]
        want = {t["id"] for t in fail}
        mir.apply(changes)
        r = ver.verify(req, stop_on_fail=False, wall_s=120, changed=list(changes))
        got = {i for i, o in r.results.items() if o == "fail"}
        ok1 = got == want and not r.not_run and not r.died
        mir.restore()
        r2 = ver.verify(req, stop_on_fail=False, wall_s=120, changed=list(changes))
        ok2 = all(o == "pass" for o in r2.results.values()) and not r2.not_run
        print(cid, "files", files, "patched-ok", ok1, "pristine-ok", ok2, "cpu %.2f %.2f" % (r.cpu_s, r2.cpu_s),
              "n", len(req), "nfail", len(got), file=sys.stderr)
        if not ok1:
            print("  want", sorted(want), "got", sorted(got), "not_run", r.not_run, "died", r.died, r.error, file=sys.stderr)
        ok_all &= ok1 and ok2
    print("ALL OK" if ok_all else "FAILURES", file=sys.stderr)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["6ze8", "bj7g", "535z", "2vdj"]))
