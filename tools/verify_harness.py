"""Verify br.harness against the sample answer key.
usage: python tools/verify_harness.py <patch-id|none> [--only-file F] [--pool real]"""
import json, os, shutil, subprocess, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
def main():
    pid = sys.argv[1]
    only = sys.argv[sys.argv.index("--only-file") + 1] if "--only-file" in sys.argv else None
    pool = sys.argv[sys.argv.index("--pool") + 1] if "--pool" in sys.argv else "inline"
    EXC = {"tests/test_parser.py::TestParser::test_max_nodes", "tests/test_executor.py::TestExecutor::test_execute_tpcds"}
    root = "/tmp/s"
    if os.path.exists(root):
        shutil.rmtree(root)
    shutil.copytree("/src", root, ignore=shutil.ignore_patterns(".git"))
    if pid != "none":
        subprocess.run(["git", "apply", f"/kit/sample/commits/{pid}.patch"], cwd=root, check=True)
    from br import harness
    tests = json.load(open("/kit/sample/tests.json"))["tests"]
    ans = json.load(open("/kit/sample/answers.json"))["commits"]
    want = set(ans[pid]["failing"]) if pid != "none" else set()
    sel = [t for t in tests if t["id"] not in EXC and (only is None or t["file"] == only)]
    harness.setup(root)
    t0 = time.time(); c0 = time.process_time()
    res = harness.run([t["id"] for t in sel], pool_mode=pool)
    num = {t["id"]: t["number"] for t in tests}
    got = {num[i] for i, o, _ in res if o == "fail"}
    selnums = {num[t["id"]] for t in sel}
    want &= selnums
    print(f"patch={pid} pool={pool} ran={len(res)} wall={time.time()-t0:.1f}s cpu={time.process_time()-c0:.1f}s")
    print("ours-only (we fail, key passes):", sorted(got - want))
    print("key-only (key fails, we pass):", sorted(want - got))
    print("MATCH" if got == want else "MISMATCH", len(got), len(want))
    json.dump({"res": res}, open(f"/scratch/verify_{pid}_{pool}.json", "w"))



if __name__ == "__main__":
    main()
