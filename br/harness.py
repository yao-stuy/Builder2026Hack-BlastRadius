"""Run sqlglot tests in-process with unittest. Outcome per test id: pass / fail / notrun."""
from __future__ import annotations

import concurrent.futures as _cf
import importlib
import os
import pickle
import re
import signal
import sys
import time
import unittest

_ROOT = None


class _Timeout(BaseException):
    pass


def setup(root):
    global _ROOT
    root = os.path.abspath(root)
    _ROOT = root
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONPATH"] = root  # test_lazy_load spawns a python subprocess
    import unittest as _u  # noqa: F401  must precede the first sqlglot import (UNITTEST flag)



PAD_FRAMES = 30  # bare TestSuite.run is ~6 frames deep at test entry, pytest ~32-37


def _pad(n, fn):
    if n <= 0:
        return fn()
    return _pad(n - 1, fn)


def _rt(obj):
    return pickle.loads(pickle.dumps(obj, pickle.HIGHEST_PROTOCOL))


class InlineExecutor:
    """Synchronous stand-in for ProcessPoolExecutor."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._shutdown = False
        if initializer is not None:
            initializer(*_rt(initargs))

    def submit(self, fn, /, *args, **kwargs):
        fut = _cf.Future()
        try:
            fn, args, kwargs = _rt((fn, args, kwargs))  # real pool pickles (sqlglot pickles via serde)
            res = _rt(fn(*args, **kwargs))
        except Exception as e:  # noqa: BLE001
            try:
                e = _rt(e)
            except Exception:
                pass
            fut.set_exception(e)
        else:
            fut.set_result(res)
        return fut

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [fn(*a) for a in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        self._shutdown = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown(wait=True)
        return False


FAIL_FAST = os.environ.get("BR_FAILFAST", "1") == "1"
_FIX_RE = re.compile(r"^(\w+) \((.*)\)$")
_ErrorHolder = unittest.suite._ErrorHolder
_SubTest = unittest.case._SubTest


def _registry():
    m = sys.modules.get("sqlglot.dialects.dialect")
    d = getattr(getattr(m, "Dialect", None), "_classes", None)
    return d if isinstance(d, dict) else None


def _registry_snapshot():
    """The dialect registry before a test. tests/test_dialect_entry_points.py empties it and never puts the
    entries back; every test recorded and labeled in isolation saw a registry nobody had emptied, so the
    tests that follow in this process must see one too."""
    try:
        d = _registry()
        return dict(d) if d is not None else None
    except Exception:
        return None


def _registry_repair(before):
    """Put back registry entries that the test removed (entries it added stay)."""
    try:
        d = _registry()
        if before and d is not None:
            for k, v in before.items():
                if k not in d:
                    d[k] = v
    except Exception:
        pass


class _Result(unittest.TestResult):
    def __init__(self, owner):
        super().__init__()
        self.o = owner
        # With failfast, unittest leaves a test body as soon as one of its subtests has failed. The test's
        # outcome is already decided at that point, so the remaining subtests are only cost.
        self.failfast = FAIL_FAST

    def stop(self):
        # failfast calls this on every failure; whether the whole run ends is the owner's decision.
        if self.o.stop_on_fail or not FAIL_FAST:
            self.shouldStop = True

    def startTest(self, test):
        super().startTest(test)
        self.o.start(test)

    def stopTest(self, test):
        self.o.stop(test)
        super().stopTest(test)

    def _exc_info_to_string(self, err, test):
        # Only the pass/fail outcome is used; formatting tracebacks is pure overhead.
        return ""

    def _bad(self, test):
        if isinstance(test, _SubTest):
            test = test.test_case
        self.o.bad(test)

    def addError(self, test, err):
        super().addError(test, err)
        self._bad(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._bad(test)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.o.bad(test)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._bad(test)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        if isinstance(test, _ErrorHolder):
            self.o.fixture_skip(test)


class _Run:
    def __init__(self, timeout_s, stop_on_fail):
        self.timeout_s = timeout_s
        self.stop_on_fail = stop_on_fail
        self.out = {}
        self.cpu = {}
        self.by_inst = {}    # id(test instance) -> test id string
        self.keep = []
        self.class_ids = {}  # "module.Class" -> [ids]
        self.module_ids = {}  # "module" -> [ids]
        self.skipped_fix = set()
        self.cur = None
        self.last = None
        self.mark = 0.0
        self.result = None
        self.registry = None

    def stopping(self):
        return self.result is not None and self.result.shouldStop

    def _limit(self, tid):
        t = self.timeout_s
        if t is None:
            return None
        try:
            return float(t(tid) if callable(t) else t)
        except Exception:
            return None

    def start(self, test):
        tid = self.by_inst.get(id(test))
        if tid is None:
            return
        self.cur = tid
        self.registry = _registry_snapshot()
        lim = self._limit(tid)
        if lim and lim > 0:
            signal.setitimer(signal.ITIMER_REAL, lim, 0.2)

    def stop(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        tid = self.by_inst.get(id(test))
        if tid is None:
            return
        _registry_repair(self.registry)
        self.registry = None
        now = time.process_time()
        self.cpu[tid] = self.cpu.get(tid, 0.0) + (now - self.mark)
        self.mark = now
        self.out.setdefault(tid, "pass")
        self.last = tid
        self.cur = None

    def _fail_ids(self, ids):
        for i in ids:
            self.out[i] = "fail"
        if self.stop_on_fail and self.result is not None:
            self.result.shouldStop = True

    def bad(self, test):
        tid = self.by_inst.get(id(test))
        if tid is not None:
            self._fail_ids([tid])
        elif isinstance(test, _ErrorHolder):
            kind, name = self._fixture(test)
            ids = None
            if kind and kind.endswith("Class"):
                ids = self.class_ids.get(name)
            elif kind and kind.endswith("Module"):
                ids = self.module_ids.get(name)
            if not ids:  # unknown holder: conservative
                ids = [i for v in self.class_ids.values() for i in v]
            self._fail_ids(ids)

    @staticmethod
    def _fixture(holder):
        m = _FIX_RE.match(holder.description or "")
        return (m.group(1), m.group(2)) if m else (None, None)

    def fixture_skip(self, holder):
        kind, name = self._fixture(holder)
        if kind and kind.startswith("setUp"):
            d = self.class_ids if kind.endswith("Class") else self.module_ids
            self.skipped_fix.update(d.get(name, []))


def _parse_id(tid):
    path, cls, meth = tid.split("::")
    mod = path[:-3] if path.endswith(".py") else path
    return mod.replace("/", ".").replace("\\", "."), cls, meth


def run(ids, stop_on_fail=False, timeout_s=None, pool_mode="inline"):
    ids = list(ids)
    r = _Run(timeout_s, stop_on_fail)
    try:
        _run(ids, r, pool_mode)
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        sys.stderr.write(f"harness internal error: {e!r}\n")
        if not r.stopping():
            for i in ids:
                if i not in r.out:
                    r.out[i] = "fail"
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        except Exception:
            pass
    res = []
    for i in ids:
        o = r.out.get(i)
        if o is None:
            if i in r.skipped_fix:
                o = "pass"
            else:
                o = "notrun" if (stop_on_fail and r.stopping()) else "fail"
        res.append((i, o, r.cpu.get(i, 0.0)))
    return res


def _run(ids, r, pool_mode):
    mods = {}
    for tid in ids:
        try:
            m, c, t = _parse_id(tid)
        except Exception:
            r.out[tid] = "fail"
            continue
        mods.setdefault(m, {}).setdefault(c, []).append((tid, t))

    def handler(signum, frame):
        raise _Timeout()

    old = signal.signal(signal.SIGALRM, handler)
    try:
        for modname, classes in mods.items():
            if r.stopping():
                break
            try:
                mod = importlib.import_module(modname)
                if pool_mode == "inline" and modname in ("tests.test_optimizer", "tests.test_executor"):
                    if hasattr(mod, "ProcessPoolExecutor"):
                        mod.ProcessPoolExecutor = InlineExecutor
            except BaseException as e:  # noqa: BLE001
                if isinstance(e, KeyboardInterrupt):
                    raise
                for cl in classes.values():
                    for tid, _ in cl:
                        r.out[tid] = "fail"
                continue
            suite = unittest.TestSuite()
            mod_ids = []
            for cname, items in classes.items():
                cids = [tid for tid, _ in items]
                cls = getattr(mod, cname, None)
                if cls is None or not isinstance(cls, type) or not issubclass(cls, unittest.TestCase):
                    for tid in cids:
                        r.out[tid] = "fail"
                    continue
                r.class_ids.setdefault(f"{cls.__module__}.{cls.__qualname__}", []).extend(cids)
                r.module_ids.setdefault(cls.__module__, []).extend(cids)
                mod_ids.extend(cids)
                for tid, meth in items:
                    try:
                        if not hasattr(cls, meth):
                            raise AttributeError(meth)
                        inst = cls(meth)
                    except Exception:
                        r.out[tid] = "fail"
                        continue
                    r.by_inst[id(inst)] = tid
                    r.keep.append(inst)
                    suite.addTest(inst)
            if suite.countTestCases() == 0:
                continue
            if r.result is None:
                r.result = _Result(r)
            r.mark = time.process_time()
            try:
                _pad(PAD_FRAMES, lambda: suite.run(r.result))
            except _Timeout:
                signal.setitimer(signal.ITIMER_REAL, 0)
                if r.cur:
                    r.out[r.cur] = "fail"
                for tid in mod_ids:
                    if tid not in r.out:
                        r.out[tid] = "fail"
            now = time.process_time()
            if r.last is not None:  # class/module teardown time -> last test run
                r.cpu[r.last] = r.cpu.get(r.last, 0.0) + max(0.0, now - r.mark)
            r.mark = now
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
