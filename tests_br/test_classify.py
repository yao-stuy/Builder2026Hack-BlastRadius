import difflib, os, sys, textwrap, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br import classify as C, diffparse as dp


def run(old, new, path="m.py", status=None):
    old = textwrap.dedent(old).lstrip("\n")
    new = textwrap.dedent(new).lstrip("\n")
    def marked(t):
        ls = t.splitlines(True)
        if ls and not ls[-1].endswith("\n"):
            ls[-1] += "\n\\ No newline at end of file\n"
        return ls
    d = "".join(difflib.unified_diff(marked(old), marked(new), "a/" + path, "b/" + path, n=1))
    fps = dp.parse("diff --git a/%s b/%s\n%s" % (path, path, d)) if d else [dp.FilePatch(path, path)]
    fp = fps[0]
    if status:
        fp.status = status
    assert dp.apply(fp, old) == new
    return C.classify(path, old, new, fp), old


SRC = """\
import os
X = 1


class A:
    K = {"a": lambda v: v + 1,
         "b": 2}
    Z = 5

    def f(self, x=3):
        '''doc'''
        y = x + 1
        if y > 2:
            return y
        else:
            return 0

    @property
    def g(self):
        try:
            return 1
        except ValueError as e:
            return 2
        finally:
            pass


def top(a):
    return [
        a,
        a,
    ]
"""


def mut(a, b, src=SRC):
    assert a in src, a
    return src.replace(a, b, 1)


class T(unittest.TestCase):
    def test_noop_cases(self):
        for new in (
            mut("X = 1", "# hello\nX = 1"),
            mut("X = 1", "X = 1  # trailing"),
            mut("X = 1\n", "X = 1\n\n"),
            mut("X = 1", "X  =  1"),
            mut('"b": 2}', '"b": 2,}'),
        ):
            im, _ = run(SRC, new)
            self.assertEqual(im.kind, "noop", new)
        im, _ = run(SRC, mut("X = 1", "# hello\nX = 1"))
        self.assertTrue(im.lineno_shift)
        self.assertIn("# hello", im.comment_text)
        im, _ = run(SRC, mut("X = 1", "X = 1  # same line"))
        self.assertFalse(im.lineno_shift)

    def test_coding_line(self):
        im, _ = run("# a\nX = 1\n", "# a\n# -*- coding: latin-1 -*-\nX = 1\n")
        self.assertEqual(im.kind, "whole_file")
        im, _ = run("# -*- coding: latin-1 -*-\nX = 1\n", "# -*- coding: utf-8 -*-\nX = 1\n")
        self.assertEqual(im.kind, "whole_file")
        im, _ = run("# -*- coding: latin-1 -*-\nX = 1\n", "Y = 0\n# -*- coding: latin-1 -*-\nX = 1\n")
        self.assertEqual(im.kind, "whole_file")

    def test_syntax_error(self):
        im, _ = run(SRC, mut("return y", "return (y"))
        self.assertEqual(im.kind, "syntax_error")
        im, _ = run("def f():\n    pass\n", "return 1\n")
        self.assertEqual(im.kind, "syntax_error")

    def test_class_level_const(self):
        im, old = run(SRC, mut("Z = 5", "Z = 6"))
        self.assertEqual((im.kind, im.level), ("lines", "top"))
        self.assertEqual(im.old_lines, {8})
        self.assertEqual(im.scopes, ["A"])

    def test_fn_line(self):
        im, _ = run(SRC, mut("y = x + 1", "y = x - 1"))
        self.assertEqual((im.kind, im.level, im.old_lines, im.scopes), ("lines", "fn", {12}, ["A.f"]))

    def test_compound_header_only(self):
        im, _ = run(SRC, mut("if y > 2:", "if y > 3:"))
        self.assertEqual(im.old_lines, {13})
        self.assertEqual(im.level, "fn")

    def test_multiline_stmt_whole(self):
        im, _ = run(SRC, mut("        a,\n        a,\n", "        a,\n        b,\n"))
        self.assertEqual(im.old_lines, {29, 30, 31, 32})
        self.assertEqual(im.scopes, ["top"])

    def test_default_arg_is_top(self):
        im, _ = run(SRC, mut("x=3", "x=4"))
        self.assertEqual((im.level, im.old_lines), ("top", {10}))

    def test_decorator_is_top(self):
        im, _ = run(SRC, mut("@property", "@staticmethod"))
        self.assertEqual(im.level, "top")
        self.assertEqual(im.old_lines, {18, 19})

    def test_lambda_body_fn_vs_key_top(self):
        im, _ = run(SRC, mut("v + 1", "v + 2"))
        self.assertEqual(im.level, "fn")
        self.assertEqual(im.old_lines, {6})
        im, _ = run(SRC, mut('"a": lambda', '"c": lambda'))
        self.assertEqual(im.level, "top")
        self.assertEqual(im.old_lines, {6, 7})
        im, _ = run(SRC, mut('lambda v: v', 'lambda w: w'))  # params: not body-only
        self.assertEqual(im.level, "top")

    def test_docstring_not_noop(self):
        im, _ = run(SRC, mut("'''doc'''", "'''docs'''"))
        self.assertEqual(im.kind, "lines")
        self.assertIn(10, im.old_lines)  # def header
        self.assertIn(11, im.old_lines)
        im, _ = run('"""m"""\nX = 1\n', '"""n"""\nX = 1\n')
        self.assertEqual(im.kind, "whole_file")

    def test_insert_in_function(self):
        im, _ = run(SRC, mut("        y = x + 1\n", "        y = x + 1\n        y += 1\n"))
        self.assertEqual((im.kind, im.level), ("lines", "fn"))
        self.assertEqual(im.old_lines, set(range(11, 17)))
        self.assertEqual(im.scopes, ["A.f"])

    def test_insert_in_multiline_call(self):
        im, _ = run(SRC, mut("        a,\n        a,\n", "        a,\n        a,\n        a,\n"))
        self.assertEqual(im.old_lines, {29, 30, 31, 32})

    def test_insert_new_method_in_class(self):
        im, _ = run(SRC, mut("    @property", "    def h(self):\n        return 1\n\n    @property"))
        self.assertEqual((im.kind, im.level), ("lines", "top"))
        self.assertEqual(im.old_lines, {5})

    def test_insert_module_level(self):
        im, _ = run(SRC, mut("X = 1", "X = 1\nW = 2"))
        self.assertEqual(im.kind, "whole_file")

    def test_insert_else_branch_and_new_stmt_in_try(self):
        im, _ = run(SRC, mut("            pass\n", "            pass\n            q = 1\n"))
        self.assertEqual(im.kind, "lines")
        self.assertEqual(im.scopes, ["A.g"])
        self.assertEqual(im.level, "fn")

    def test_delete_function(self):
        im, _ = run(SRC, mut("    def f(self, x=3):\n        '''doc'''\n        y = x + 1\n        if y > 2:\n            return y\n        else:\n            return 0\n\n", ""))
        self.assertEqual(im.kind, "lines")
        self.assertTrue({10, 11, 12, 13, 14, 15, 16} <= im.old_lines)

    def test_structural(self):
        for st in ("delete", "add", "rename"):
            im, _ = run("X = 1\n", "X = 1\n", status=st)
            self.assertEqual((im.kind, im.structural), ("whole_file", True))

    def test_future_and_unparsable_old(self):
        im, _ = run("from __future__ import annotations\nX = 1\n", "X = 1\n")
        self.assertEqual(im.kind, "whole_file")
        im, _ = run("def (:\n", "X = 1\n")
        self.assertEqual(im.kind, "whole_file")

    def test_nonpython(self):
        im, _ = run("a\nb\nc\n", "a\nB\nc\nd\n", path="f.sql")
        self.assertEqual(im.kind, "nonpython")
        self.assertEqual(im.old_lines, {2})
        self.assertEqual(im.insert_points, [(2, 3), (3, 4)])

    def test_never_raises(self):
        fp = dp.FilePatch("m.py", "m.py")
        self.assertEqual(C.classify("m.py", None, None, fp).kind, "whole_file")  # type: ignore[arg-type]
        self.assertEqual(C.classify("m.py", "x=1\n", "x=2\n", None).kind, "whole_file")  # type: ignore[arg-type]

    def test_crlf_and_no_newline(self):
        im, _ = run("def f():\r\n    return 1\r\n", "def f():\r\n    return 2\r\n")
        self.assertEqual(im.old_lines, {2})
        im, _ = run("x = 1\nif x:\n    y = 1", "x = 1\nif x:\n    y = 2")
        self.assertEqual(im.old_lines, {3})


if __name__ == "__main__":
    unittest.main()
