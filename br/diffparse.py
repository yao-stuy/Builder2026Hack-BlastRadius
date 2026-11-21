"""Git-style unified diff parsing and strict, fuzz-free patch application.

Only ``PatchError`` escapes from the public functions on malformed input.
Text is split on ``\\n`` only; ``\\r`` stays part of the line content, so CRLF
files round-trip exactly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class PatchError(Exception):
    pass


@dataclass
class Line:
    tag: str  # ' ', '-' or '+'
    text: str  # content without the trailing "\n" (a "\r" is kept)
    old_no: int | None
    new_no: int | None
    no_newline: bool = False  # this line has no terminating "\n" in its file


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[Line] = field(default_factory=list)


@dataclass
class FilePatch:
    old_path: str | None = None
    new_path: str | None = None
    status: str = "modify"  # modify | add | delete | rename
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    copy: bool = False  # status == "rename" but the source file stays
    old_mode: str | None = None
    new_mode: str | None = None
    similarity: int | None = None

    @property
    def path(self) -> str | None:
        return self.new_path if self.new_path is not None else self.old_path

    @property
    def mode_only(self) -> bool:
        return (
            not self.hunks
            and not self.binary
            and self.status == "modify"
            and self.old_mode != self.new_mode
        )


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_ESC = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}


def _unquote(tok: str) -> str:
    """Decode a git C-style quoted path (tok includes surrounding quotes)."""
    if len(tok) < 2 or tok[0] != '"' or tok[-1] != '"':
        raise PatchError(f"bad quoted path: {tok!r}")
    s = tok[1:-1]
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 1
            if i >= len(s):
                raise PatchError("dangling backslash in quoted path")
            c = s[i]
            if c in "01234567":
                j = i
                while j < len(s) and j < i + 3 and s[j] in "01234567":
                    j += 1
                v = int(s[i:j], 8)
                if v > 255:
                    raise PatchError("bad octal escape")
                out.append(v)
                i = j
                continue
            if c in _ESC:
                out.append(_ESC[c])
            else:
                raise PatchError(f"bad escape \\{c}")
        else:
            out += c.encode("utf-8")
        i += 1
    return out.decode("utf-8", "surrogateescape")


def _split_quoted_prefix(s: str) -> tuple[str, str]:
    """s starts with a quote; return (quoted token, rest)."""
    i = 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            return s[: i + 1], s[i + 1 :]
        i += 1
    raise PatchError("unterminated quoted path")


def _path_token(s: str) -> str:
    """Path from a ---/+++/rename line payload (may be quoted, may have a tab suffix)."""
    s = s.rstrip("\r")
    if s.startswith('"'):
        tok, _ = _split_quoted_prefix(s)
        return _unquote(tok)
    if "\t" in s:
        s = s.split("\t", 1)[0]
    return s


def _strip_prefix(p: str) -> str:
    if p.startswith(("a/", "b/")):
        return p[2:]
    return p


def _parse_diff_git_line(rest: str) -> tuple[str | None, str | None]:
    """Paths (prefix stripped) from the text after 'diff --git '."""
    rest = rest.rstrip("\r")
    if rest.startswith('"'):
        a_tok, rest2 = _split_quoted_prefix(rest)
        a = _unquote(a_tok)
        rest2 = rest2.lstrip(" ")
        if rest2.startswith('"'):
            b_tok, _ = _split_quoted_prefix(rest2)
            b = _unquote(b_tok)
        else:
            b = rest2
        return _strip_prefix(a), _strip_prefix(b)
    # unquoted a: find a split " b/" where both halves agree; else first " b/"
    cands = [m.start() for m in re.finditer(r" (?=(?:b/|\"))", rest)]
    first = None
    for pos in cands:
        a, b = rest[:pos], rest[pos + 1 :]
        if b.startswith('"'):
            try:
                b = _unquote(b)
            except PatchError:
                continue
            b_stripped = _strip_prefix(b)
        else:
            b_stripped = _strip_prefix(b)
        if first is None:
            first = (_strip_prefix(a), b_stripped)
        if _strip_prefix(a) == b_stripped:
            return _strip_prefix(a), b_stripped
    if first is not None:
        return first
    # fall back: split in the middle at a space
    if " " in rest:
        a, b = rest.split(" ", 1)
        return _strip_prefix(a), _strip_prefix(b)
    return None, None


def parse(text: str) -> list[FilePatch]:
    try:
        return _parse(text)
    except PatchError:
        raise
    except Exception as e:  # never leak anything else
        raise PatchError(f"malformed patch: {e!r}") from e


def _parse(text: str) -> list[FilePatch]:
    if not isinstance(text, str):
        raise PatchError("patch must be text")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    n = len(lines)
    i = 0
    out: list[FilePatch] = []
    cur: FilePatch | None = None
    cur_has_minus = False  # saw a '--- ' line for cur

    def finish() -> None:
        nonlocal cur
        if cur is not None:
            out.append(cur)
        cur = None

    while i < n:
        ln = lines[i]
        if ln.startswith("diff --git "):
            finish()
            cur = FilePatch()
            cur_has_minus = False
            cur.old_path, cur.new_path = _parse_diff_git_line(ln[len("diff --git ") :])
            i += 1
            continue
        if cur is not None and not cur.hunks and not cur.binary:
            handled = True
            if ln.startswith("new file mode "):
                cur.status = "add"
                cur.new_mode = ln.split()[-1]
            elif ln.startswith("deleted file mode "):
                cur.status = "delete"
                cur.old_mode = ln.split()[-1]
            elif ln.startswith("old mode "):
                cur.old_mode = ln.split()[-1]
            elif ln.startswith("new mode "):
                cur.new_mode = ln.split()[-1]
            elif ln.startswith("similarity index "):
                try:
                    cur.similarity = int(ln.split()[-1].rstrip("%"))
                except ValueError:
                    pass
            elif ln.startswith("dissimilarity index "):
                pass
            elif ln.startswith("rename from "):
                cur.status = "rename"
                cur.old_path = _path_token(ln[len("rename from ") :])
            elif ln.startswith("rename to "):
                cur.status = "rename"
                cur.new_path = _path_token(ln[len("rename to ") :])
            elif ln.startswith("copy from "):
                cur.status = "rename"
                cur.copy = True
                cur.old_path = _path_token(ln[len("copy from ") :])
            elif ln.startswith("copy to "):
                cur.status = "rename"
                cur.copy = True
                cur.new_path = _path_token(ln[len("copy to ") :])
            elif ln.startswith("index "):
                pass
            elif ln.startswith("Binary files ") or ln.startswith("GIT binary patch"):
                cur.binary = True
            else:
                handled = False
            if handled:
                i += 1
                if cur.binary:
                    # swallow the binary payload up to the next file header
                    while i < n and not lines[i].startswith("diff --git "):
                        i += 1
                continue
        # ---/+++ pair
        if ln.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks or cur_has_minus:
                finish()
                cur = FilePatch()
            cur_has_minus = True
            op = _path_token(ln[4:])
            np_ = _path_token(lines[i + 1][4:])
            if op == "/dev/null":
                cur.old_path = None
                if cur.status == "modify":
                    cur.status = "add"
            else:
                cur.old_path = _strip_prefix(op)
            if np_ == "/dev/null":
                cur.new_path = None
                if cur.status == "modify":
                    cur.status = "delete"
            else:
                cur.new_path = _strip_prefix(np_)
            if cur.status == "add":
                cur.old_path = None
            if cur.status == "delete":
                cur.new_path = None
            i += 2
            continue
        m = _HUNK_RE.match(ln)
        if m and cur is not None:
            os_, oc, ns, nc = m.groups()
            h = Hunk(int(os_), 1 if oc is None else int(oc), int(ns), 1 if nc is None else int(nc))
            i += 1
            old_no = h.old_start if h.old_count > 0 else h.old_start + 1
            new_no = h.new_start if h.new_count > 0 else h.new_start + 1
            ro, rn = h.old_count, h.new_count
            while ro > 0 or rn > 0:
                if i >= n:
                    raise PatchError("truncated hunk")
                l2 = lines[i]
                if l2.startswith("\\"):
                    raise PatchError("unexpected '\\' marker inside hunk")
                tag = l2[:1]
                body = l2[1:]
                if l2 == "":  # some tools strip the single space of empty context lines
                    tag, body = " ", ""
                if tag == " ":
                    if ro <= 0 or rn <= 0:
                        raise PatchError("hunk longer than its header says")
                    h.lines.append(Line(" ", body, old_no, new_no))
                    old_no += 1
                    new_no += 1
                    ro -= 1
                    rn -= 1
                elif tag == "-":
                    if ro <= 0:
                        raise PatchError("hunk longer than its header says")
                    h.lines.append(Line("-", body, old_no, None))
                    old_no += 1
                    ro -= 1
                elif tag == "+":
                    if rn <= 0:
                        raise PatchError("hunk longer than its header says")
                    h.lines.append(Line("+", body, None, new_no))
                    new_no += 1
                    rn -= 1
                else:
                    raise PatchError(f"bad hunk line: {l2[:40]!r}")
                i += 1
                if i < n and lines[i].startswith("\\"):
                    h.lines[-1].no_newline = True
                    i += 1
            # a trailing marker after the last line is consumed above; a marker may
            # also follow after we reach zero counts (handled by the in-loop check)
            cur.hunks.append(h)
            continue
        # anything else outside hunks (commit message text, "-- " signature...) is ignored
        i += 1
    finish()
    for fp in out:
        if fp.old_path is None and fp.new_path is None:
            raise PatchError("file patch without a path")
        if fp.status in ("modify", "rename") and (fp.old_path is None or fp.new_path is None):
            if fp.old_path is None:
                fp.old_path = fp.new_path
            else:
                fp.new_path = fp.old_path
        prev = 0
        for h in fp.hunks:
            if h.old_start < prev:
                raise PatchError("hunks out of order")
            prev = h.old_start + h.old_count
    return out


def removed_old_lines(fp: FilePatch) -> list[int]:
    return [ln.old_no for h in fp.hunks for ln in h.lines if ln.tag == "-"]  # type: ignore[misc]


def insert_points(fp: FilePatch) -> list[tuple[int, int]]:
    """(old line just before, old line just after) for each run of '+' lines."""
    pts: list[tuple[int, int]] = []
    for h in fp.hunks:
        last_old = h.old_start - 1 if h.old_count > 0 else h.old_start
        in_run = False
        for ln in h.lines:
            if ln.tag == "+":
                if not in_run:
                    pts.append((last_old, last_old + 1))
                    in_run = True
            else:
                in_run = False
                last_old = ln.old_no  # type: ignore[assignment]
    return pts


def _split_keep(text: str) -> list[tuple[str, bool]]:
    """Split on '\\n' into (content, has_newline) pairs."""
    if text == "":
        return []
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
        return [(p, True) for p in parts]
    return [(p, True) for p in parts[:-1]] + [(parts[-1], False)]


def apply(fp: FilePatch, old_text: str | None) -> str:
    """Apply one file patch strictly (no fuzz, no offset). Returns the new text."""
    if fp.binary:
        raise PatchError("cannot apply a binary patch")
    if old_text is None:
        old_text = ""
    old = _split_keep(old_text)
    out: list[tuple[str, bool]] = []
    pos = 0  # next unconsumed index in old
    for h in fp.hunks:
        start = h.old_start - 1 if h.old_count > 0 else h.old_start
        if start < pos or start > len(old):
            raise PatchError(f"hunk @@ -{h.old_start} out of range")
        out.extend(old[pos:start])
        pos = start
        for ln in h.lines:
            if ln.tag in (" ", "-"):
                if pos >= len(old):
                    raise PatchError(f"hunk @@ -{h.old_start}: file too short")
                content, nl = old[pos]
                if content != ln.text or nl == ln.no_newline:
                    raise PatchError(f"hunk @@ -{h.old_start}: mismatch at old line {pos + 1}")
                pos += 1
                if ln.tag == " ":
                    out.append((content, nl))
            else:
                out.append((ln.text, not ln.no_newline))
        # a line without newline can only be last; enforce
    out.extend(old[pos:])
    for idx, (_, nl) in enumerate(out):
        if not nl and idx != len(out) - 1:
            raise PatchError("line without newline in the middle of the file")
    return "".join(c + ("\n" if nl else "") for c, nl in out)
