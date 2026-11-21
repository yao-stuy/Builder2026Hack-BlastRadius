"""Classify a per-file patch: which OLD lines must a test execute to be affected?

``classify(path, old_text, new_text, fp) -> Impact``; never raises (any internal
error yields ``kind="whole_file"``).  Conservative by construction: when in doubt
widen the line set or give up with ``whole_file``.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
import warnings
from dataclasses import dataclass, field

from . import diffparse

_CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SKIP_TOK = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENDMARKER,
    tokenize.ENCODING,
}


@dataclass
class Impact:
    kind: str  # noop | lines | whole_file | syntax_error | nonpython
    old_lines: set[int] = field(default_factory=set)
    scopes: list[str] = field(default_factory=list)
    level: str | None = None  # fn | top | mixed (kind == "lines" only)
    lineno_shift: bool = False
    comment_text: str = ""
    structural: bool = False
    insert_points: list[tuple[int, int]] = field(default_factory=list)
    reason: str = ""


# --------------------------------------------------------------------------- helpers


class _Give(Exception):
    """Cannot classify safely -> whole_file."""


def _code_lines(text: str) -> set[int]:
    out: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in _SKIP_TOK:
                continue
            out.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        for i, ln in enumerate(text.split("\n"), 1):
            s = ln.strip()
            if s and not s.startswith("#"):
                out.add(i)
    return out


def _start(n: ast.AST) -> int:
    if isinstance(n, (*_DEFS, ast.ClassDef)):
        return min([n.lineno] + [d.lineno for d in n.decorator_list])
    return n.lineno  # type: ignore[attr-defined]


def _end(n: ast.AST) -> int:
    return n.end_lineno  # type: ignore[attr-defined]


def _child_groups(n: ast.AST) -> list[list[ast.stmt]]:
    groups: list[list[ast.stmt]] = []
    for f in ("body", "orelse", "finalbody"):
        v = getattr(n, f, None)
        if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
            groups.append(v)
    for h in getattr(n, "handlers", None) or []:
        groups.append(h.body)
    for c in getattr(n, "cases", None) or []:
        groups.append(c.body)
    return groups


_CHILD_FIELDS = {"body", "orelse", "finalbody", "handlers", "cases"}


def _own_nodes(n: ast.AST):
    """Expression nodes belonging to the statement itself (not nested statements)."""
    for name, val in ast.iter_fields(n):
        if name in _CHILD_FIELDS and (
            name != "body" or (isinstance(val, list) and val and isinstance(val[0], ast.stmt))
        ):
            continue
        vals = val if isinstance(val, list) else [val]
        for v in vals:
            if isinstance(v, ast.AST):
                yield from ast.walk(v)


@dataclass
class _Unit:
    key: tuple
    lines: frozenset
    node: ast.AST  # statement (or Try for handlers) whose position decides scope/level
    kind: str  # stmt | hdr | handler | gap
    self_scope: bool  # node itself is a def/class whose header this unit is
    chain: list


class _Index:
    def __init__(self, text: str, tree: ast.Module):
        self.text = text
        self.lines = text.split("\n")
        self.tree = tree
        self.code = _code_lines(text)
        self.parent: dict[int, ast.AST] = {}
        self.qual: dict[int, str] = {}
        self.by_qual: dict[str, list[ast.AST]] = {}
        self.lambdas: dict[str, int] = {}
        self._walk(tree, tree, "")

    def _walk(self, node: ast.AST, parent: ast.AST, prefix: str) -> None:
        for group in ([node.body] if isinstance(node, ast.Module) else _child_groups(node)):
            for s in group:
                self.parent[id(s)] = node
                child_prefix = prefix
                if isinstance(s, (*_DEFS, ast.ClassDef)):
                    q = prefix + s.name
                    self.qual[id(s)] = q
                    self.by_qual.setdefault(q, []).append(s)
                    child_prefix = q + (".<locals>." if isinstance(s, _DEFS) else ".")
                self._walk(s, node, child_prefix)

    # -- positions
    def line_text(self, n: int) -> str:
        return self.lines[n - 1] if 1 <= n <= len(self.lines) else ""

    def _trim_hdr(self, first_line: int, hdr_end: int) -> int:
        while hdr_end > first_line:
            s = self.line_text(hdr_end).strip()
            if s == "" or s.startswith("#"):
                hdr_end -= 1
            else:
                break
        return hdr_end

    def hdr_lines(self, n: ast.AST) -> range:
        st = _start(n)
        firsts = []
        if isinstance(getattr(n, "body", None), list) and n.body and isinstance(n.body[0], ast.stmt):
            firsts.append(_start(n.body[0]))
        for c in getattr(n, "cases", None) or []:
            firsts.append(c.pattern.lineno)
        if not firsts:
            return range(st, _end(n) + 1)
        f = min(firsts)
        end = f if f == _node_header_line(n) else f - 1
        if f <= st:
            end = st
        return range(st, self._trim_hdr(st, max(end, st)) + 1)

    def handler_hdr(self, h: ast.ExceptHandler) -> range:
        f = _start(h.body[0])
        end = f if f == h.lineno else f - 1
        return range(h.lineno, self._trim_hdr(h.lineno, max(end, h.lineno)) + 1)

    def is_fn_ctx(self, n: ast.AST) -> bool:
        """Is statement n located inside some function body (strict ancestors)?"""
        p = self.parent.get(id(n))
        while p is not None and not isinstance(p, ast.Module):
            if isinstance(p, _DEFS):
                return True
            p = self.parent.get(id(p))
        return False

    def scope_of(self, n: ast.AST, self_scope: bool) -> str:
        if self_scope and id(n) in self.qual:
            return self.qual[id(n)]
        p = self.parent.get(id(n))
        while p is not None and not isinstance(p, ast.Module):
            if id(p) in self.qual:
                return self.qual[id(p)]
            p = self.parent.get(id(p))
        return "<module>"

    def find_unit(self, L: int) -> _Unit | None:
        groups: list[list[ast.stmt]] = [self.tree.body]
        cur: ast.AST | None = None
        chain: list[ast.AST] = []
        while True:
            found = None
            for g in groups:
                for s in g:
                    if _start(s) <= L <= _end(s):
                        found = s
                        break
                if found is not None:
                    break
            if found is None:
                break
            cur = found
            chain.append(cur)
            groups = _child_groups(cur)
            if not groups and not _is_compound(cur):
                return _Unit((id(cur), "stmt"), frozenset(range(_start(cur), _end(cur) + 1)), cur,
                             "stmt", False, chain)
        if cur is None:
            return None
        hdr = self.hdr_lines(cur)
        selfscope = isinstance(cur, (*_DEFS, ast.ClassDef))
        if L in hdr:
            return _Unit((id(cur), "hdr"), frozenset(hdr), cur, "hdr", selfscope, chain)
        for h in getattr(cur, "handlers", None) or []:
            if h.lineno <= L <= _end(h):
                hh = self.handler_hdr(h)
                if L in hh:
                    return _Unit((id(h), "handler"), frozenset(hh), cur, "handler", False, chain)
                if L in self.code:
                    return _Unit((id(h), "gap"), frozenset(hh) | {L}, cur, "gap", False, chain)
                return None
        if L in self.code:
            return _Unit((id(cur), "gap", L), frozenset(hdr) | {L}, cur, "gap", selfscope, chain)
        return None

    def is_docstring(self, u: _Unit) -> ast.AST | None:
        n = u.node
        if u.kind != "stmt" or not isinstance(n, ast.Expr):
            return None
        v = n.value
        if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
            return None
        p = self.parent.get(id(n))
        if isinstance(p, (ast.Module, *_DEFS, ast.ClassDef)) and p.body and p.body[0] is n:
            return p
        return None


def _is_compound(n: ast.AST) -> bool:
    return any(
        isinstance(getattr(n, f, None), list) and getattr(n, f)
        and isinstance(getattr(n, f)[0], (ast.stmt, ast.ExceptHandler, ast.match_case))
        for f in ("body", "orelse", "finalbody", "handlers", "cases")
    )


def _node_header_line(n: ast.AST) -> int:
    return n.lineno  # type: ignore[attr-defined]


def _future_set(tree: ast.Module) -> list[str]:
    out = []
    for s in tree.body:
        if isinstance(s, ast.ImportFrom) and s.module == "__future__":
            out.append(ast.dump(s))
    return out


# --------------------------------------------------------------------------- runs


@dataclass
class _Run:
    removed: list[tuple[int, str]]
    added: list[tuple[int, str]]
    before: int
    after: int


def _runs(fp: diffparse.FilePatch) -> list[_Run]:
    runs: list[_Run] = []
    for h in fp.hunks:
        last_old = h.old_start - 1 if h.old_count > 0 else h.old_start
        cur: _Run | None = None
        for ln in h.lines:
            if ln.tag == " ":
                if cur is not None:
                    cur.after = last_old + 1
                    runs.append(cur)
                    cur = None
                last_old = ln.old_no  # type: ignore[assignment]
                continue
            if cur is None:
                cur = _Run([], [], last_old, last_old + 1)
            if ln.tag == "-":
                cur.removed.append((ln.old_no, ln.text))  # type: ignore[arg-type]
                last_old = ln.old_no  # type: ignore[assignment]
            else:
                cur.added.append((ln.new_no, ln.text))  # type: ignore[arg-type]
        if cur is not None:
            cur.after = last_old + 1
            runs.append(cur)
    return runs


def _byte_col(idx: _Index, line: int, col: int) -> int:
    return len(idx.line_text(line)[:col].encode("utf-8", "surrogateescape"))


def _run_old_span(idx: _Index, r: _Run) -> tuple[tuple[int, int], tuple[int, int]]:
    """Changed character span of the run in OLD coordinates as ((line,bytecol),(line,bytecol))."""
    if not r.removed:
        p = (r.after, 0)
        return p, p
    bo = "\n".join(t for _, t in r.removed)
    bn = "\n".join(t for _, t in r.added)
    p = 0
    m = min(len(bo), len(bn))
    while p < m and bo[p] == bn[p]:
        p += 1
    s = 0
    while s < m - p and bo[len(bo) - 1 - s] == bn[len(bn) - 1 - s]:
        s += 1
    first = r.removed[0][0]

    def pos(off: int) -> tuple[int, int]:
        ln = first + bo.count("\n", 0, off)
        col = off - (bo.rfind("\n", 0, off) + 1)
        return ln, _byte_col(idx, ln, col)

    return pos(p), pos(len(bo) - s)


# --------------------------------------------------------------------------- main


def classify(path: str, old_text: str, new_text: str, fp: diffparse.FilePatch) -> Impact:
    try:
        return _classify(path, old_text, new_text, fp)
    except _Give as g:
        return Impact("whole_file", reason=str(g), structural=_structural(fp))
    except BaseException as e:  # noqa: BLE001 - must never raise
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        return Impact("whole_file", reason=f"internal error: {e!r}", structural=_structural(fp))


def _structural(fp: diffparse.FilePatch) -> bool:
    try:
        return fp.status != "modify"
    except Exception:  # noqa: BLE001
        return False


def _classify(path: str, old_text: str, new_text: str, fp: diffparse.FilePatch) -> Impact:
    if not path.endswith(".py"):
        return Impact(
            "nonpython",
            old_lines=set(diffparse.removed_old_lines(fp)),
            insert_points=diffparse.insert_points(fp),
            structural=_structural(fp),
            reason="not a .py file",
        )
    if fp.binary:
        raise _Give("binary patch on a .py path")
    if fp.status != "modify":
        raise _Give(f"python file {fp.status}")
    if old_text is None or new_text is None:
        raise _Give("missing text")
    if re.search(r"\r(?!\n)", old_text) or re.search(r"\r(?!\n)", new_text):
        raise _Give("lone CR line endings")

    # new source must compile
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compile(new_text, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError, MemoryError, OverflowError) as e:
        return Impact("syntax_error", reason=repr(e))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            old_tree = ast.parse(old_text)
            new_tree = ast.parse(new_text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise _Give("old file does not parse") from None

    runs = _runs(fp)
    ipts = diffparse.insert_points(fp)

    # PEP 263 cookie: first two lines changed or shifted
    o2 = old_text.split("\n", 2)[:2]
    n2 = new_text.split("\n", 2)[:2]
    if o2 != n2 and any(_CODING_RE.match(x) for x in o2 + n2):
        raise _Give("encoding declaration touched")
    for r in runs:
        for no, t in r.removed + r.added:
            if no <= 2 and _CODING_RE.match(t):
                raise _Give("encoding declaration touched")

    shift = any(len(r.removed) != len(r.added) for r in runs)
    added_text = "\n".join(t.strip() for r in runs for _, t in r.added)

    if ast.dump(old_tree) == ast.dump(new_tree):
        pos_shift = False
        for a, b in zip(ast.walk(old_tree), ast.walk(new_tree)):
            if getattr(a, "lineno", None) != getattr(b, "lineno", None) or getattr(
                a, "end_lineno", None
            ) != getattr(b, "end_lineno", None):
                pos_shift = True
                break
        return Impact("noop", lineno_shift=pos_shift,
                      comment_text=added_text, insert_points=ipts, reason="identical AST")

    if _future_set(old_tree) != _future_set(new_tree):
        raise _Give("__future__ imports changed")

    old = _Index(old_text, old_tree)
    new = _Index(new_text, new_tree)

    lines: set[int] = set()
    scopes: set[str] = set()
    levels: list[str] = []

    def add_unit(u: _Unit, span: tuple[tuple[int, int], tuple[int, int]]) -> None:
        doc = old.is_docstring(u)
        if doc is not None:
            if isinstance(doc, ast.Module):
                raise _Give("module docstring changed")
            lines.update(old.hdr_lines(doc))
        scopes.add(old.scope_of(u.node, u.self_scope))
        lvl = "fn" if old.is_fn_ctx(u.node) else "top"
        narrowed = None
        if lvl == "top" and u.kind in ("stmt", "hdr"):
            for n in _own_nodes(u.node):
                if isinstance(n, ast.Lambda):
                    b = n.body
                    lo = (b.lineno, b.col_offset)
                    hi = (b.end_lineno, b.end_col_offset)
                    if lo <= span[0] and span[1] <= hi:
                        # change lies wholly inside a lambda body: only tests that
                        # call the lambda can see it (innermost such lambda wins)
                        cand = range(n.lineno, n.end_lineno + 1)
                        if narrowed is None or len(cand) <= len(narrowed):
                            narrowed = cand
                        lvl = "fn"
        lines.update(narrowed if narrowed is not None else u.lines)
        levels.append(lvl)

    for r in runs:
        span = _run_old_span(old, r)
        old_units: dict[tuple, _Unit] = {}
        for no, _ in r.removed:
            u = old.find_unit(no)
            if u is not None:
                old_units[u.key] = u
        for u in old_units.values():
            add_unit(u, span)
        added_code = [no for no, _ in r.added if no in new.code]
        if not added_code:
            continue
        new_keys = set()
        for no in added_code:
            nu = new.find_unit(no)
            if nu is None:
                raise _Give("added code line outside any statement")
            new_keys.add(nu.key)
        if old_units and len(new_keys) <= len(old_units):
            continue  # in-place modification of existing statements
        if not r.removed:
            b, a = r.before, r.after
            if 1 <= b and a <= len(old.lines):
                ub, ua = old.find_unit(b), old.find_unit(a)
                if ub is not None and ua is not None and ub.key == ua.key and ub.kind in ("stmt", "hdr"):
                    add_unit(ub, span)
                    continue
        # genuine insertion: map through the NEW tree to enclosing old function/class
        for no in added_code:
            nu = new.find_unit(no)
            assert nu is not None
            containers = []
            for n in reversed(nu.chain):
                if isinstance(n, (*_DEFS, ast.ClassDef)) and _start(n.body[0]) <= no <= _end(n):
                    containers.append(n)
            if not containers:
                raise _Give("module-level insertion")
            done = False
            for ci, n in enumerate(containers):
                any_fn = any(isinstance(m, _DEFS) for m in containers[ci:])
                cps = old.by_qual.get(new.qual[id(n)])
                if not cps:
                    continue
                for cp in cps:
                    if isinstance(cp, _DEFS):
                        lines.update(range(_start(cp.body[0]), _end(cp) + 1))
                    else:
                        lines.update(old.hdr_lines(cp))
                    scopes.add(old.qual[id(cp)])
                levels.append("fn" if any_fn else "top")
                done = True
                break
            if not done:
                raise _Give("module-level insertion")

    if not lines:
        raise _Give("AST differs but no executable lines mapped")
    lines = {x for x in lines if 1 <= x <= len(old.lines)}
    if not lines:
        raise _Give("mapped lines out of range")
    lv = set(levels)
    level = "fn" if lv == {"fn"} else "top" if lv == {"top"} else "mixed"
    return Impact("lines", old_lines=lines, scopes=sorted(scopes), level=level,
                  lineno_shift=shift, comment_text=added_text, insert_points=ipts)
