"""Split tests over 1..8 workers minimizing  max worker load + 2000 ms * non-empty workers.

``best`` = LPT per worker count k, a bounded move/swap improvement pass, and an
exact branch-and-bound for small inputs.  Pure stdlib, no recursion on input size.
"""

from __future__ import annotations

import heapq
import math
import time
from bisect import bisect_left
from collections.abc import Mapping, Sequence

WORKER_MS = 2000
MAX_WORKERS = 8
_EXACT_N = 14  # exact search only when the input is this small
_EXACT_NODES = 40000
_IMPROVE_BUDGET_S = 0.0015


def _ms(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)


def cost_ms(workers: Sequence[Sequence[int]], durations: Mapping[int, float]) -> int:
    """Exact scorer arithmetic. durations: test number -> recorded SECONDS (as in tests.json).

    load = sum of round(seconds*1000) (halves up); 0 if nothing runs, else
    max load + 2000 per non-empty worker.
    """
    ms = {k: _ms(v) for k, v in durations.items()}
    return cost_of_ms(workers, ms)


def cost_of_ms(workers: Sequence[Sequence[int]], ms_by_number: Mapping[int, int]) -> int:
    occupied = [w for w in workers if w]
    if not occupied:
        return 0
    return max(sum(ms_by_number[t] for t in w) for w in occupied) + WORKER_MS * len(occupied)


def _lpt(items: list[tuple[int, int]], k: int) -> tuple[list[list[tuple[int, int]]], list[int]]:
    bins: list[list[tuple[int, int]]] = [[] for _ in range(k)]
    heap = [(0, i) for i in range(k)]
    loads = [0] * k
    for it in items:  # items sorted by duration desc
        load, i = heap[0]
        bins[i].append(it)
        load += it[1]
        loads[i] = load
        heapq.heapreplace(heap, (load, i))
    return bins, loads


def _improve(bins: list[list[tuple[int, int]]], loads: list[int], lb: int) -> None:
    """Move/swap between the busiest and other workers while the makespan drops."""
    deadline = time.perf_counter() + _IMPROVE_BUDGET_S
    k = len(bins)
    for _ in range(200):
        if max(loads) <= lb or time.perf_counter() > deadline:
            return
        b = max(range(k), key=loads.__getitem__)
        lb_load = loads[b]
        best_gain = 0
        best_move = None
        bsorted = sorted(bins[b], key=lambda t: t[1])
        bvals = [t[1] for t in bsorted]
        for w in range(k):
            if w == b:
                continue
            gap = lb_load - loads[w]
            if gap <= 1:
                continue
            half = gap / 2
            # candidate x to move: duration < gap, closest to gap/2
            j = bisect_left(bvals, half)
            for jj in (j - 1, j):
                if 0 <= jj < len(bvals):
                    x = bvals[jj]
                    if 0 < x < gap:
                        new_max = max(lb_load - x, loads[w] + x)
                        gain = lb_load - new_max
                        if gain > best_gain:
                            best_gain, best_move = gain, (w, bsorted[jj], None)
            # swap x (in b) with y (in w): 0 < x - y < gap, x - y closest to gap/2
            wsorted = sorted(bins[w], key=lambda t: t[1])
            wvals = [t[1] for t in wsorted]
            if not wvals:
                continue
            # bound the work on huge workers: look at a window of candidates
            step = max(1, len(bsorted) // 64)
            for ix in range(0, len(bsorted), step):
                x = bvals[ix]
                target = x - half
                jy = bisect_left(wvals, target)
                for jj in (jy - 1, jy, jy + 1):
                    if 0 <= jj < len(wvals):
                        d = x - wvals[jj]
                        if 0 < d < gap:
                            new_max = max(lb_load - d, loads[w] + d)
                            gain = lb_load - new_max
                            if gain > best_gain:
                                best_gain, best_move = gain, (w, bsorted[ix], wsorted[jj])
        if best_move is None:
            return
        w, xi, yi = best_move
        bins[b].remove(xi)
        bins[w].append(xi)
        loads[b] -= xi[1]
        loads[w] += xi[1]
        if yi is not None:
            bins[w].remove(yi)
            bins[b].append(yi)
            loads[w] -= yi[1]
            loads[b] += yi[1]


def _exact(items: list[tuple[int, int]], k: int, ub: int) -> list[list[tuple[int, int]]] | None:
    """Branch and bound for min makespan with <= k bins; returns a strictly better packing or None."""
    n = len(items)
    loads = [0] * k
    assign = [0] * n
    best: list[int] | None = None
    best_mk = ub
    suffix = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = suffix[i + 1] + items[i][1]
    total = suffix[0]
    nodes = 0
    # iterative DFS
    stack = [(0, 0)]  # (item index, next bin to try)
    choice = [-1] * n
    while stack:
        i, start = stack.pop()
        if i == n:
            mk = max(loads)
            if mk < best_mk:
                best_mk = mk
                best = assign[:]
            continue
        if start > 0:  # undo previous placement of item i
            p = choice[i]
            loads[p] -= items[i][1]
        d = items[i][1]
        for bi in range(start, k):
            nodes += 1
            if nodes > _EXACT_NODES:
                return _materialize(items, best, k)
            # symmetry: skip bins whose load equals an earlier bin's load
            if any(loads[bj] == loads[bi] for bj in range(bi)):
                continue
            nl = loads[bi] + d
            if nl >= best_mk:
                continue
            # lower bound: remaining work spread over all bins
            if (total_remaining_lb(loads, suffix[i + 1], nl, bi, k)) >= best_mk:
                continue
            loads[bi] = nl
            assign[i] = bi
            choice[i] = bi
            stack.append((i, bi + 1))
            stack.append((i + 1, 0))
            break
    return _materialize(items, best, k)


def total_remaining_lb(loads: list[int], rest: int, nl: int, bi: int, k: int) -> int:
    s = rest + sum(loads) - loads[bi] + nl
    return -(-s // k)


def _materialize(items, assign, k):
    if assign is None:
        return None
    bins: list[list[tuple[int, int]]] = [[] for _ in range(k)]
    for it, a in zip(items, assign):
        bins[a].append(it)
    return bins


def _cost(bins: list[list[tuple[int, int]]]) -> int:
    occ = [sum(t[1] for t in b) for b in bins if b]
    return (max(occ) + WORKER_MS * len(occ)) if occ else 0


def best(tests: Sequence[tuple[int, int]], exact: bool = True) -> list[list[int]]:
    """tests: (test number, duration ms) -> list of workers (lists of test numbers)."""
    if not tests:
        return []
    items = sorted(((int(a), int(b)) for a, b in tests), key=lambda t: (-t[1], t[0]))
    n = len(items)
    total = sum(t[1] for t in items)
    top = items[0][1]
    kmax = min(MAX_WORKERS, n)
    lbs = {k: max(top, -(-total // k)) + WORKER_MS * k for k in range(1, kmax + 1)}
    best_cost = None
    best_bins = None
    for k in sorted(lbs, key=lambda k: (lbs[k], k)):
        if best_cost is not None and lbs[k] >= best_cost:
            continue
        bins, loads = _lpt(items, k)
        lb = max(top, -(-total // k))
        if max(loads) > lb:
            _improve(bins, loads, lb)
        if exact and n <= _EXACT_N and max(loads) > lb:
            ex = _exact(items, k, max(loads))
            if ex is not None:
                bins = ex
        c = _cost(bins)
        if best_cost is None or c < best_cost or (c == best_cost and sum(1 for b in bins if b) < sum(1 for b in best_bins if b)):
            best_cost, best_bins = c, bins
    assert best_bins is not None
    return [[t[0] for t in b] for b in best_bins if b]
