import json, sys, os
sys.path.insert(0, "/repo")
from br import harness, adapters
harness.setup("/tmp/p")
T = {t["number"]: t["id"] for t in json.load(open("/kit/sample/tests.json"))["tests"]}
fail = json.load(open("/kit/sample/answers.json"))["commits"]["2jrq"]["failing"]
print([T[n] for n in fail if "executor" in T[n]])
ids = [i for i in T.values() if "test_executor.py::TestExecutor" in i and not i.endswith("tpcds")]
import tests.test_executor
if sys.argv[1] == "lazy": print(adapters.install_executor_lazy())
res = harness.run(ids)
print("fails:", sorted(i.split("::")[-1] for i, o, c in res if o != "pass"))
