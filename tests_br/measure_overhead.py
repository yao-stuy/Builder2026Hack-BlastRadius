import json, os, shutil, sys, time, statistics, compileall, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
mode = sys.argv[1]
tid = sys.argv[2]
from br.overlay import Mirror
from br.engine import Verifier
t0 = time.process_time()
pre = ["unittest"] + (["duckdb", "pandas", "numpy"] if mode == "heavy" else [])
v = Verifier("/tmp/m", preload=pre)
t1 = time.process_time()
r = v.verify([tid], stop_on_fail=False)
t2 = time.process_time()
print(mode, tid.split("::")[0], "parent_preload_cpu %.3f verify_childcpu %.3f parent_cpu_in_verify %.4f wall %.3f res %s" % (t1 - t0, r.cpu_s, t2 - t1, r.wall_s, r.results), file=sys.stderr)
