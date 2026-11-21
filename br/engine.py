"""Fork-per-verification engine: run test ids against a mirror in a throw-away child."""
from __future__ import annotations

import importlib
import json
import os
import select
import signal
import sys
import time
from dataclasses import dataclass, field


GC_FREEZE = os.environ.get("BR_GC_FREEZE", "1") == "1"


@dataclass
class VerifyResult:
    results: dict = field(default_factory=dict)
    not_run: list = field(default_factory=list)
    first_fail: str | None = None
    cpu_s: float = 0.0
    wall_s: float = 0.0
    died: bool = False
    timed_out: bool = False
    suspect: str | None = None
    error: str | None = None
    cpus: dict = field(default_factory=dict)


def _parse_id(tid):
    path, cls, meth = tid.split("::")
    mod = path[:-3] if path.endswith(".py") else path
    return mod.replace("/", ".").replace("\\", "."), cls, meth


def _groups(test_ids):
    """Group ids by (module, class) in order of first appearance."""
    groups = {}
    for tid in test_ids:
        try:
            m, c, _ = _parse_id(tid)
        except Exception:
            m, c = "?", tid
        groups.setdefault((m, c), []).append(tid)
    return list(groups.values())


class Verifier:
    def __init__(self, mirror_root, preload=(), stderr="discard", harness_module=None):
        self.root = os.path.abspath(mirror_root)
        self.stderr = stderr  # "discard" | "keep" | file path
        self.harness = harness_module
        self.loaded = set()
        self.warm_mods = set()
        self._preload_std()
        self.ensure_preloaded(preload)
        try:
            self._harness()  # import br.harness in the parent so children do not
        except Exception:  # noqa: BLE001
            pass

    # Stdlib / third-party modules that many tests import (directly or via unittest.mock).
    # Importing them in the parent is outcome-neutral and saves ~0.1 s CPU per child.
    _STD_PRELOAD = ("unittest", "unittest.mock", "asyncio", "pickle", "decimal", "fractions",
                    "difflib", "tracemalloc", "tempfile", "subprocess", "sysconfig", "pytz",
                    "zoneinfo", "dateutil.relativedelta", "dateutil.parser")

    def _preload_std(self):
        for n in self._STD_PRELOAD:
            try:
                importlib.import_module(n)
                self.loaded.add(n)
            except Exception:  # noqa: BLE001
                pass

    def ensure_preloaded(self, names):
        for n in names:
            if n in self.loaded:
                continue
            try:
                importlib.import_module(n)
                self.loaded.add(n)
            except BaseException as e:  # noqa: BLE001
                if isinstance(e, KeyboardInterrupt):
                    raise
                sys.stderr.write("preload %s failed: %r\n" % (n, e))

    def warm(self, names):
        """OPT-IN: import modules (e.g. 'sqlglot', 'tests.dialects.test_dialect') in the
        parent FROM THE PRISTINE MIRROR so that children skip that import cost.
        Call only while the mirror is pristine. verify(changed=...) then makes the child
        discard the warm modules if a changed file could affect them."""
        before = set(sys.modules)
        if self.root not in sys.path:
            sys.path.insert(0, self.root)
        cwd = os.getcwd()
        try:
            os.chdir(self.root)
            os.environ.setdefault("SKIP_INTEGRATION", "0")
            for n in names:
                try:
                    importlib.import_module(n)
                except BaseException as e:  # noqa: BLE001
                    if isinstance(e, KeyboardInterrupt):
                        raise
                    sys.stderr.write("warm %s failed: %r\n" % (n, e))
        finally:
            os.chdir(cwd)
        self.warm_mods |= {m for m in set(sys.modules) - before
                           if (getattr(sys.modules[m], "__file__", None) or "").startswith(self.root + os.sep)}

    @staticmethod
    def _rel_module(rel):
        if not rel.endswith(".py"):
            return None
        parts = rel[:-3].replace("\\", "/").split("/")
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    def _purge_if_stale(self, changed):
        if not self.warm_mods:
            return
        stale = changed is None or any(
            self._rel_module(c) in self.warm_mods for c in changed if c.endswith(".py"))
        if stale:
            for m in self.warm_mods:
                sys.modules.pop(m, None)
            self.warm_mods = set()

    def _harness(self):
        if self.harness is not None:
            return self.harness
        from . import harness as h
        return h

    # ------------------------------------------------------------------
    def verify(self, test_ids, *, stop_on_fail=True, timeouts=None, wall_s=60.0, changed=None,
               prepare=None, specials=None, always=()):
        """prepare(harness): called in the child after harness.setup, before any test runs.
        specials: {test_id: callable -> "pass" | "fail" | None}; those ids are not run as tests,
        the callable decides their outcome (None = no verdict). Specials run after the normal
        tests and are not skipped by stop_on_fail.
        always: ids that run first and never stop the run (their verdict is wanted regardless)."""
        test_ids = list(test_ids)
        res = VerifyResult()
        t0 = time.monotonic()
        try:
            self._verify(test_ids, stop_on_fail, timeouts, wall_s, res, changed, prepare, specials or {}, set(always))
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, KeyboardInterrupt):
                raise
            res.died = True
            res.error = repr(e)
        res.wall_s = time.monotonic() - t0
        res.not_run = [t for t in test_ids if t not in res.results]
        if (res.died or res.timed_out) and res.suspect is None and res.not_run:
            res.suspect = res.not_run[0]
        return res

    def _verify(self, test_ids, stop_on_fail, timeouts, wall_s, res, changed, prepare=None, specials=None, always=()):
        if not test_ids:
            return
        rfd, wfd = os.pipe()
        try:
            pid = os.fork()
        except BaseException:
            os.close(rfd)
            os.close(wfd)
            raise
        if pid == 0:
            self._child(rfd, wfd, test_ids, stop_on_fail, timeouts, changed, prepare, specials or {}, always)  # never returns
        os.close(wfd)
        started = None
        buf = b""
        status = None
        deadline = time.monotonic() + wall_s
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    res.timed_out = True
                    break
                try:
                    r, _, _ = select.select([rfd], [], [], left)
                    if not r:
                        res.timed_out = True
                        break
                    chunk = os.read(rfd, 65536)
                except InterruptedError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    try:
                        m = json.loads(ln)
                    except Exception:
                        continue
                    if "s" in m:
                        started = m["s"]
                    elif "r" in m:
                        tid, o = m["r"], m["o"]
                        if "c" in m:
                            res.cpus[tid] = m["c"]
                        if res.results.get(tid) != "fail":
                            res.results[tid] = o
                        if o == "fail" and res.first_fail is None:
                            res.first_fail = tid
                        if started == tid:
                            started = None
                    elif "x" in m:
                        if started == m["x"]:
                            started = None
                    elif "e" in m:
                        res.error = m["e"]
        finally:
            try:
                os.close(rfd)
            except OSError:
                pass
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            while True:
                try:
                    _, status, ru = os.wait4(pid, 0)
                    res.cpu_s = ru.ru_utime + ru.ru_stime
                    break
                except InterruptedError:
                    continue
                except ChildProcessError:
                    break
        if status is None or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
            res.died = True
        if started is not None and started not in res.results and (res.died or res.timed_out):
            res.suspect = started

    # ------------------------------------------------------------------
    def _child(self, rfd, wfd, test_ids, stop_on_fail, timeouts, changed, prepare=None, specials=None, always=()):
        code = 1
        try:
            os.setsid()
            for s in (signal.SIGINT, signal.SIGTERM, signal.SIGPIPE, signal.SIGALRM):
                try:
                    signal.signal(s, signal.SIG_DFL)
                except Exception:
                    pass
            os.close(rfd)
            dn = os.open(os.devnull, os.O_RDWR)
            os.dup2(dn, 0)
            os.dup2(dn, 1)
            if self.stderr == "discard":
                os.dup2(dn, 2)
            elif self.stderr not in ("keep", None):
                fd = os.open(self.stderr, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                os.dup2(fd, 2)
                os.close(fd)
            if dn > 2:
                os.close(dn)
            try:
                with open("/proc/self/oom_score_adj", "w") as f:
                    f.write("1000")
            except Exception:
                pass

            def emit(obj):
                os.write(wfd, (json.dumps(obj) + "\n").encode())

            self._purge_if_stale(changed)
            h = self._harness()
            h.setup(self.root)
            if GC_FREEZE:
                # Everything inherited from the parent (interpreter, pandas, duckdb, warm sqlglot) is live for
                # the whole run; keep the collector from re-scanning it on every collection.
                import gc
                gc.freeze()
            specials = specials or {}
            if prepare is not None:
                prepare(h)
            self._install_hook(emit)
            if timeouts is None or callable(timeouts) or isinstance(timeouts, (int, float)):
                tmo = timeouts
            else:
                d = dict(timeouts)
                tmo = lambda tid: d.get(tid)  # noqa: E731
            first = [t for t in test_ids if t in always and t not in specials]
            for grp in _groups(first):
                for tid, o, _cpu in h.run(grp, stop_on_fail=False, timeout_s=tmo):
                    if o != "notrun":
                        emit({"r": tid, "o": o, "c": round(_cpu, 3)})
            for grp in _groups([t for t in test_ids if t not in specials and t not in always]):
                out = h.run(grp, stop_on_fail=stop_on_fail, timeout_s=tmo)
                stop = False
                for tid, o, _cpu in out:
                    if o == "notrun":
                        continue
                    emit({"r": tid, "o": o, "c": round(_cpu, 3)})
                    if o == "fail" and stop_on_fail:
                        stop = True
                if stop:
                    break
            for tid in test_ids:
                fn = specials.get(tid)
                if fn is None:
                    continue
                emit({"s": tid})
                t0 = time.process_time()
                try:
                    o = fn()
                except BaseException:  # noqa: BLE001
                    o = None
                if o in ("pass", "fail"):
                    emit({"r": tid, "o": o, "c": round(time.process_time() - t0, 3)})
                else:
                    emit({"x": tid})
            code = 0
        except BaseException as e:  # noqa: BLE001
            try:
                os.write(wfd, (json.dumps({"e": repr(e)}) + "\n").encode())
            except Exception:
                pass
        finally:
            os._exit(code)

    @staticmethod
    def _install_hook(emit):
        """Wrap TestCase.run to announce each test before it starts and its
        pass/fail right after, so partial results survive a hang or crash."""
        import unittest
        orig = unittest.TestCase.run

        def run(self, result=None):
            cls = type(self)
            tid = "%s::%s::%s" % (cls.__module__.replace(".", "/") + ".py",
                                  cls.__qualname__, self._testMethodName)
            try:
                emit({"s": tid})
            except Exception:
                pass
            if result is None:
                return orig(self, result)
            n0 = (len(result.failures), len(result.errors), len(result.unexpectedSuccesses))
            ret = orig(self, result)
            n1 = (len(result.failures), len(result.errors), len(result.unexpectedSuccesses))
            try:
                emit({"r": tid, "o": "fail" if n1 != n0 else "pass"})
            except Exception:
                pass
            return ret

        unittest.TestCase.run = run
