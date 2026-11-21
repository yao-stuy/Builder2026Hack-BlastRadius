import sys, time, json
sys.path.insert(0, "/repo")
from br import harness, adapters
harness.setup(sys.argv[1])
out = {}
try:
    import tests.test_parser
    t = time.process_time(); out["proxy"] = adapters.max_nodes_proxy(); out["proxy_cpu"] = round(time.process_time() - t, 2)
    r = harness.run(["tests/test_parser.py::TestParser::test_max_nodes"], timeout_s=300)
    out["real"] = r[0][1]; out["real_cpu"] = round(r[0][2], 1)
except BaseException as e:
    out["err"] = repr(e)
print("RESULT", json.dumps(out))
