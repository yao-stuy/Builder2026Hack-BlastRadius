"""Lines of a function/lambda/generator code object that never produce a sys.monitoring LINE event
(CPython does not report a LINE event for code sitting on the definition line itself).  Attribute them
on function entry (PY_START): every line from co_firstlineno (first decorator) through the line of the
first body instruction.  Stdlib only."""
import dis

_RESUME = dis.opmap["RESUME"]


def entry_lines(code):
    first = code.co_firstlineno
    co = code.co_code
    resume = 0
    for i in range(0, len(co), 2):
        if co[i] == _RESUME:
            resume = i
            break
    resume_line = first
    body_line = None
    for start, end, line in code.co_lines():
        if line is None:
            continue
        if start <= resume < end:
            resume_line = line
        if start > resume and body_line is None:
            body_line = line
    last = max(resume_line, body_line if body_line is not None else resume_line)
    if last < first or last - first > 200:  # defensive
        last = first
    return range(first, last + 1)
