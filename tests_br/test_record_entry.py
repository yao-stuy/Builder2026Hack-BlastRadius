"""Function-entry attribution (tools/entrylines.py): every physical line of a lambda/def header or body
must be covered by LINE events + PY_START entry lines once the function is called."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from entrylines import entry_lines  # noqa: E402

SRC = '''class A:
    T = {
        "ONE": lambda self: self.f(rename=True),
        "MULTI": lambda self, query: self.g(
            query, [1], self.h()
        ),
    }

    def one(self): return 1

    @staticmethod
    @dec
    def deco(
        a,
        b,
    ):
        return a

    def gen(self):
        yield 1
        yield 2


def outer():
    return lambda x: (
        x
        + 1
    )
'''

LAM_ONE, LAM_MULTI = 3, (4, 5)
DEF_ONE = 9
DECO = (11, 12, 13, 14, 15, 16, 17)
GEN = (19, 20, 21)
NESTED = (25, 26, 27)


def _record(fn_call):
    mon = sys.monitoring
    tool = next(i for i in range(6) if mon.get_tool(i) is None)
    mon.use_tool_id(tool, "t_entry")
    hits = set()
    line_only = set()

    def on_line(code, line):
        if code.co_filename == "<syn>":
            hits.add(line)
            line_only.add(line)
        return mon.DISABLE

    def on_start(code, off):
        if code.co_filename == "<syn>" and code.co_flags & 0x1:  # CO_OPTIMIZED
            hits.update(entry_lines(code))
        return mon.DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.register_callback(tool, mon.events.PY_START, on_start)
    mon.set_events(tool, mon.events.LINE | mon.events.PY_START)
    try:
        ns = {"dec": lambda f: f}
        exec(compile(SRC, "<syn>", "exec"), ns)
        hits.clear()  # forget import-time (top) hits
        fn_call(ns)
    finally:
        mon.set_events(tool, 0)
        mon.register_callback(tool, mon.events.LINE, None)
        mon.register_callback(tool, mon.events.PY_START, None)
        mon.free_tool_id(tool)
    return hits


class S:
    def f(self, **k):
        return 1

    def g(self, *a):
        return 1

    def h(self):
        return 1


class EntryTest(unittest.TestCase):
    def test_one_line_lambda(self):
        h = _record(lambda ns: ns["A"].T["ONE"](S()))
        self.assertIn(LAM_ONE, h)

    def test_multi_line_lambda(self):
        h = _record(lambda ns: ns["A"].T["MULTI"](S(), 1))
        self.assertTrue(set(LAM_MULTI) <= h, h)

    def test_one_line_def(self):
        h = _record(lambda ns: ns["A"].one(None))
        self.assertIn(DEF_ONE, h)

    def test_decorated_multiline_def(self):
        h = _record(lambda ns: ns["A"].deco(1, 2))
        self.assertTrue(set(DECO) <= h, h)

    def test_generator(self):
        h = _record(lambda ns: list(ns["A"].gen(None)))
        self.assertTrue(set(GEN) <= h, h)

    def test_nested_lambda(self):
        h = _record(lambda ns: ns["outer"]()(1))
        self.assertTrue(set(NESTED) <= h, h)

    def test_not_called_not_recorded(self):
        h = _record(lambda ns: None)
        self.assertFalse(h)


if __name__ == "__main__":
    unittest.main()
