import sys, time
sys.path.insert(0, "/repo")
from br import harness, adapters
harness.setup(sys.argv[1] if len(sys.argv) > 1 else "/src")
import tests.test_parser
t=time.process_time(); r=adapters.max_nodes_proxy(); t1=time.process_time()-t
t=time.process_time(); r2=adapters.max_nodes_proxy(); t2=time.process_time()-t
print(r, round(t1,3), r2, round(t2,3))
