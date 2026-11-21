import sys, time, os
sys.path.insert(0, "/src"); import unittest
import tests.test_executor as te
t=time.process_time()
c=te.duckdb.connect()
for table, columns in te.TPCH_SCHEMA.items():
    c.execute(f"CREATE VIEW {table} AS SELECT * FROM READ_CSV('{te.DIR_TPCH}{table}.csv.gz', delim='|', header=True, columns={columns})")
print("tpch views", time.process_time()-t)
t=time.process_time()
c=te.duckdb.connect()
for table, columns in te.TPCDS_SCHEMA.items():
    c.execute(f"CREATE VIEW {table} AS SELECT * FROM READ_CSV('{te.DIR_TPCDS}{table}.csv.gz', delim='|', header=True, columns={columns})")
print("tpcds views", time.process_time()-t)
t=time.process_time()
te.TestExecutor.setUpClass()
print("setUpClass", time.process_time()-t)
