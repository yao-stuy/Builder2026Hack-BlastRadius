"""Engine tests (stand-in harness for robustness; real harness in test_engine_e2e)."""
import os, signal, sys, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br.engine import Verifier


class FakeHarness:
    @staticmethod
    def setup(root):
        os.chdir(root if os.path.isdir(root) else "/")

    @staticmethod
    def run(ids, stop_on_fail=False, timeout_s=None):
        out = []
        for i in ids:
            if "hang" in i:
                time.sleep(1000)
            if "crash" in i:
                os.kill(os.getpid(), signal.SIGSEGV)
            if "noisy" in i:
                os.write(1, b"STDOUT LEAK\n")
            o = "fail" if "fail" in i else "pass"
            out.append((i, o, 0.0))
            if o == "fail" and stop_on_fail:
                out += [(j, "notrun", 0.0) for j in ids[len(out):]]
                break
        return out


def T(c, m):
    return "tests/test_x.py::%s::%s" % (c, m)


class TestEngine(unittest.TestCase):
    def v(self):
        return Verifier("/tmp", harness_module=FakeHarness)

    def test_pass_fail(self):
        r = self.v().verify([T("A", "a"), T("A", "fail1"), T("B", "b")], stop_on_fail=False)
        self.assertEqual(r.results, {T("A", "a"): "pass", T("A", "fail1"): "fail", T("B", "b"): "pass"})
        self.assertEqual(r.first_fail, T("A", "fail1"))
        self.assertFalse(r.died or r.timed_out)
        self.assertEqual(r.not_run, [])

    def test_stop_on_fail(self):
        r = self.v().verify([T("A", "fail1"), T("B", "b")], stop_on_fail=True)
        self.assertEqual(r.not_run, [T("B", "b")])
        self.assertFalse(r.died)

    def test_crash(self):
        r = self.v().verify([T("A", "a"), T("B", "crash"), T("C", "c")])
        self.assertTrue(r.died)
        self.assertEqual(r.results, {T("A", "a"): "pass"})
        self.assertEqual(r.not_run, [T("B", "crash"), T("C", "c")])
        self.assertEqual(r.suspect, T("B", "crash"))

    def test_hang(self):
        t = time.time()
        r = self.v().verify([T("A", "a"), T("B", "hang")], wall_s=0.5)
        self.assertLess(time.time() - t, 3)
        self.assertTrue(r.timed_out)
        self.assertEqual(r.suspect, T("B", "hang"))
        self.assertEqual(r.results, {T("A", "a"): "pass"})

    def test_no_stdout(self):
        rfd, wfd = os.pipe()
        saved = os.dup(1)
        os.dup2(wfd, 1)
        try:
            self.v().verify([T("A", "noisy")])
        finally:
            os.dup2(saved, 1); os.close(saved); os.close(wfd)
        os.set_blocking(rfd, False)
        try:
            data = os.read(rfd, 100)
        except BlockingIOError:
            data = b""
        os.close(rfd)
        self.assertEqual(data, b"")

    def test_empty_and_bad_root(self):
        self.assertEqual(self.v().verify([]).not_run, [])
        r = Verifier("/nonexistent/zzz").verify([T("A", "a")])  # real harness import may fail -> died
        self.assertEqual(r.not_run, [T("A", "a")])

    def test_no_leaks(self):
        v = self.v()
        v.verify([T("A", "a")])
        n0 = len(os.listdir("/proc/self/fd"))
        for i in range(200):
            v.verify([T("A", "a"), T("B", "fail")], stop_on_fail=bool(i % 2))
        self.assertEqual(len(os.listdir("/proc/self/fd")), n0)
        with self.assertRaises(ChildProcessError):
            os.wait()  # no zombies/children
        v.verify([T("A", "hang")], wall_s=0.2)
        v.verify([T("A", "crash")])
        self.assertEqual(len(os.listdir("/proc/self/fd")), n0)
        with self.assertRaises(ChildProcessError):
            os.wait()


if __name__ == "__main__":
    unittest.main()
