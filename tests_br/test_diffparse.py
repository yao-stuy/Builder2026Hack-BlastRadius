import pathlib, os, shutil, subprocess, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br import diffparse as dp


def git_diff(files_old, files_new, extra=()):
    """Build a real git diff between two dicts of {path: bytes} (needs git)."""
    d = tempfile.mkdtemp()
    try:
        def run(*a):
            return subprocess.run(["git", "-c", "core.quotepath=true", *a], cwd=d, capture_output=True, check=True).stdout
        run("init", "-q")
        run("config", "user.email", "x@x"); run("config", "user.name", "x")
        for p, b in files_old.items():
            os.makedirs(os.path.dirname(os.path.join(d, p)) or d, exist_ok=True)
            with open(os.path.join(d, p), "wb") as fh:
                fh.write(b)
        run("add", "-A"); run("commit", "-qm", "a")
        for p in list(files_old):
            if p not in files_new:
                os.remove(os.path.join(d, p))
        for p, b in files_new.items():
            os.makedirs(os.path.dirname(os.path.join(d, p)) or d, exist_ok=True)
            with open(os.path.join(d, p), "wb") as fh:
                fh.write(b)
        run("add", "-A")
        return run("diff", "--cached", "-M", "--binary", *extra).decode("utf-8")
    finally:
        shutil.rmtree(d, ignore_errors=True)


HAVE_GIT = shutil.which("git") is not None


def roundtrip(tc, old, new, **kw):
    patch = git_diff(old, new, **kw)
    fps = dp.parse(patch)
    return patch, fps


@unittest.skipUnless(HAVE_GIT, "git needed")
class RealGit(unittest.TestCase):
    def check_all(self, old, new):
        patch, fps = roundtrip(self, old, new)
        for fp in fps:
            if fp.binary:
                continue
            o = old.get(fp.old_path, b"").decode() if fp.old_path else ""
            if fp.copy:
                o = old[fp.old_path].decode()
            self.assertEqual(dp.apply(fp, o), new[fp.new_path].decode() if fp.new_path else "")
        return fps

    def test_modify_multi_hunk(self):
        a = "".join(f"line{i}\n" for i in range(100))
        b = a.replace("line5\n", "FIVE\n").replace("line50\n", "line50\nextra\n").replace("line90\n", "")
        fps = self.check_all({"x.py": a.encode()}, {"x.py": b.encode()})
        self.assertEqual(len(fps[0].hunks), 3)
        self.assertEqual(dp.removed_old_lines(fps[0]), [6, 91])
        self.assertEqual(dp.insert_points(fps[0]), [(6, 7), (51, 52)])

    def test_add_delete(self):
        fps = self.check_all({"d.py": b"a\nb\n"}, {"n/new.py": b"x\ny\n"})
        st = {f.path: f.status for f in fps}
        self.assertEqual(st, {"d.py": "delete", "n/new.py": "add"})
        d = [f for f in fps if f.status == "delete"][0]
        self.assertIsNone(d.new_path)
        a = [f for f in fps if f.status == "add"][0]
        self.assertIsNone(a.old_path)

    def test_rename_pure_and_edit(self):
        body = "".join(f"l{i}\n" for i in range(30))
        fps = self.check_all({"a.py": body.encode()}, {"b.py": body.encode()})
        self.assertEqual((fps[0].status, fps[0].old_path, fps[0].new_path, fps[0].hunks), ("rename", "a.py", "b.py", []))
        fps = self.check_all({"a.py": body.encode()}, {"b.py": body.replace("l7\n", "L7\n").encode()})
        self.assertEqual(fps[0].status, "rename")
        self.assertEqual(len(fps[0].hunks), 1)

    def test_no_newline(self):
        for o, n in [("a\nb", "a\nb\n"), ("a\nb\n", "a\nb"), ("a\nb", "a\nc"), ("a", "a\nb")]:
            self.check_all({"f.txt": o.encode()}, {"f.txt": n.encode()})

    def test_crlf(self):
        self.check_all({"f.txt": b"a\r\nb\r\nc\r\n"}, {"f.txt": b"a\r\nB\r\nc\r\nd\r\n"})

    def test_quoted_and_space_paths(self):
        self.check_all({"sp ace/é.py": b"1\n2\n"}, {"sp ace/é.py": b"1\n3\n"})
        self.check_all({"a b.py": b"1\n2\n"}, {"a b.py": b"1\n3\n"})

    def test_binary_and_mode(self):
        fps = dp.parse(git_diff({"b.bin": b"\x00\x01\x02" * 50}, {"b.bin": b"\x00\x05\x02" * 50}))
        self.assertTrue(fps[0].binary)
        self.assertEqual(fps[0].path, "b.bin")
        d = tempfile.mkdtemp()
        try:
            def run(*a): return subprocess.run(["git", *a], cwd=d, capture_output=True, check=True).stdout
            run("init", "-q"); run("config", "user.email", "x@x"); run("config", "user.name", "x")
            open(os.path.join(d, "s.sh"), "w").write("echo\n")
            run("add", "-A"); run("commit", "-qm", "a")
            run("update-index", "--chmod=+x", "s.sh")
            fps = dp.parse(run("diff", "--cached").decode())
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertTrue(fps[0].mode_only)
        self.assertEqual(dp.apply(fps[0], "echo\n"), "echo\n")


class Synthetic(unittest.TestCase):
    def test_strict_mismatch(self):
        p = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
        fp = dp.parse(p)[0]
        self.assertEqual(dp.apply(fp, "a\nb\n"), "a\nc\n")
        with self.assertRaises(dp.PatchError):
            dp.apply(fp, "a\nX\n")
        with self.assertRaises(dp.PatchError):
            dp.apply(fp, "z\na\nb\n")  # no offset tolerance
        with self.assertRaises(dp.PatchError):
            dp.apply(fp, "a\nb")  # missing trailing newline

    def test_plain_unified(self):
        p = "--- a/f\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
        fp = dp.parse(p)[0]
        self.assertEqual(fp.status, "delete")
        self.assertEqual(dp.apply(fp, "a\n"), "")

    def test_garbage_only_patch_error(self):
        import random
        rnd = random.Random(1)
        base = ("diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n-b\n+c\n d\n"
                "\\ No newline at end of file\n")
        for _ in range(3000):
            s = list(base)
            for _ in range(rnd.randint(1, 6)):
                k = rnd.randrange(len(s))
                op = rnd.random()
                if op < 0.4:
                    del s[k]
                elif op < 0.8:
                    s.insert(k, rnd.choice("@-+ \\n\"0123456789,ab/"))
                else:
                    s[k] = rnd.choice("@-+ \\n\"09")
            t = "".join(s)
            try:
                for fp in dp.parse(t):
                    try:
                        dp.apply(fp, "a\nb\nd")
                    except dp.PatchError:
                        pass
                    dp.removed_old_lines(fp); dp.insert_points(fp)
            except dp.PatchError:
                pass

    def test_empty_context_line_stripped(self):
        p = "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n\n-c\n+d\n"
        fp = dp.parse(p)[0]
        self.assertEqual(dp.apply(fp, "a\n\nc\n"), "a\n\nd\n")


if __name__ == "__main__":
    unittest.main()
