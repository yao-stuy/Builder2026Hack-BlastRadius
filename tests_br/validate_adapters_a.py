"""usage: python tests_br/validate_adapters_a.py {orig|lazy} {one|class|tpch|all} [--root /src]  (inside lab container)
Compares TestExecutor outcomes with/without br.adapters.install_executor_lazy (never runs test_execute_tpcds)."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import harness, adapters
mode, what = sys.argv[1], sys.argv[2]
root = sys.argv[sys.argv.index("--root") + 1] if "--root" in sys.argv else "/src"
harness.setup(root)
tests = json.load(open("/kit/sample/tests.json"))
tests = tests if isinstance(tests, list) else tests.get("tests", tests)
ids = [t["id"] for t in tests if "test_executor.py::TestExecutor" in t["id"] and not t["id"].endswith("test_execute_tpcds")]
if what == "one":
    ids = [i for i in ids if i.endswith("test_py_dialect")]
elif what == "tpch":
    ids = [i for i in ids if i.endswith(("test_execute_tpch", "test_optimized_tpch"))]
t0 = time.process_time()
import tests.test_executor
t1 = time.process_time()
if mode == "lazy":
    print("installed:", adapters.install_executor_lazy())
res = harness.run(ids)
t2 = time.process_time()
print(f"import {t1-t0:.2f}s run {t2-t1:.2f}s total {t2-t0:.2f}s n={len(ids)}")
print(json.dumps({i.split('::')[-1]: o for i, o, c in res}, sort_keys=True))
print(sorted(((round(c,2), i.split("::")[-1]) for i, o, c in res), reverse=True)[:8])
print("fails:", [i.split('::')[-1] for i, o, c in res if o != "pass"])
