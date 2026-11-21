"""Overlay tests. Run in the lab container: python -m unittest tests_br.test_engine_overlay"""
import compileall, os, shutil, subprocess, sys, tempfile, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br.overlay import Mirror

SRC = "/src"


def py(root, code):
    env = dict(os.environ, PYTHONPATH=root, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd="/")


@unittest.skipUnless(os.path.isdir(SRC), "needs /src")
class TestOverlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ov")
        cls.s = os.path.join(cls.tmp, "s")
        shutil.copytree(SRC, cls.s, ignore=shutil.ignore_patterns(".git"))
        compileall.compile_dir(os.path.join(cls.s, "sqlglot"), quiet=2)
        cls.m = os.path.join(cls.tmp, "m")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def snapshot(self):
        out = {}
        for d, ds, fs in os.walk(self.m):
            for n in ds + fs:
                p = os.path.join(d, n)
                out[os.path.relpath(p, self.m)] = os.readlink(p) if os.path.islink(p) else "d"
        return out

    def test_cycle(self):
        mir = Mirror(self.s, self.m)
        t = time.perf_counter(); mir.build(); build_ms = (time.perf_counter() - t) * 1000
        print("build ms", round(build_ms, 1), file=sys.stderr)
        snap = self.snapshot()
        self.assertIn("sqlglot/__pycache__", snap)
        with open(os.path.join(self.s, "sqlglot/dialects/tsql.py"), "rb") as f:
            tsql = f.read()
        r = py(self.m, "import sqlglot.dialects.tsql as t, sqlglot; print(t.__file__, t.__cached__);"
                       "print(hasattr(t,'PATCHED_MARK'))")
        self.assertEqual(r.stdout.split()[-1], "False", r.stderr)
        mir.apply({
            "sqlglot/dialects/tsql.py": tsql + b"\nPATCHED_MARK = 1\n",
            "sqlglot/newmod/__init__.py": b"X = 5\n",
            "tests/fixtures/new.sql": b"select 1;\n",
            "sqlglot/dialects/doris.py": None,  # deleted
        })
        pc = os.path.join(self.m, "sqlglot/dialects/__pycache__")
        self.assertFalse(os.path.islink(pc))
        self.assertFalse([n for n in os.listdir(pc) if n.startswith("tsql.") or n.startswith("doris.")])
        self.assertTrue(any(n.startswith("bigquery.") for n in os.listdir(pc)))
        r = py(self.m, "import sqlglot.dialects.tsql as t, sqlglot.newmod as n;print(t.PATCHED_MARK, n.X)")
        self.assertIn("1 5", r.stdout, r.stderr[-300:])
        self.assertFalse(os.path.lexists(os.path.join(self.m, "sqlglot/dialects/doris.py")))
        # patched import, unchanged modules from pyc: use -v on a variant without deletion
        mir.restore()
        self.assertEqual(self.snapshot(), snap)
        mir.apply({"sqlglot/dialects/tsql.py": tsql + b"\nPATCHED_MARK = 1\n"})
        env = dict(os.environ, PYTHONPATH=self.m, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-v", "-c", "import sqlglot.dialects.tsql as t;print(t.PATCHED_MARK)"],
                           capture_output=True, text=True, env=env, cwd="/")
        self.assertTrue(r.stdout.strip().endswith("1"), r.stderr[-500:])
        err = r.stderr
        self.assertIn("matches " + self.m + "/sqlglot/parser.py", err.replace("/src", self.m).replace(self.s, self.m)) \
            if False else None
        # unchanged module loaded from pyc, patched one compiled from source
        self.assertRegex(err, r"sqlglot/__pycache__/parser\.cpython-313\.pyc matches")
        self.assertNotRegex(err, r"dialects/__pycache__/tsql\.cpython-313\.pyc matches")
        self.assertRegex(err, r"dialects/__pycache__/bigquery\.cpython-313\.pyc matches|dialects/__pycache__/dialect\.cpython-313\.pyc matches")
        mir.restore()
        self.assertEqual(self.snapshot(), snap)
        r = py(self.m, "import sqlglot.dialects.tsql as t;print(hasattr(t,'PATCHED_MARK'))")
        self.assertEqual(r.stdout.strip(), "False", r.stderr)

    def test_failed_apply_restore(self):
        mir = Mirror(self.s, self.m); mir.build()
        snap = self.snapshot()
        with self.assertRaises(Exception):
            mir.apply({"sqlglot/a/b/c.py": b"x=1\n", "sqlglot/x.py": "notbytes"})
        mir.restore(); mir.restore()
        self.assertEqual(self.snapshot(), snap)


if __name__ == "__main__":
    unittest.main()
