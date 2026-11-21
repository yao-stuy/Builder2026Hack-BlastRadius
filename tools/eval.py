#!/usr/bin/env python3
"""Local scoring of an output.jsonl against a sample-shaped directory.

    python tools/eval.py <output.jsonl> [--sample /kit/sample] [--cpu SECONDS]

The directory holds tests.json, answers.json and commits/*.patch.  Validation and
the simulated time come from the official /kit/score.py (imported); the per-commit
table adds the oracle (optimal packing of exactly the failing tests).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br import pack  # noqa: E402

_SCORE_CANDIDATES = [
    os.environ.get("BR_SCORE_PY", ""),
    "/kit/score.py",
    str(Path.home() / "blast-radius" / "kit" / "score.py"),
    str(Path(__file__).resolve().parent.parent / "kit" / "score.py"),
]


def _load_score():
    for p in _SCORE_CANDIDATES:
        if p and Path(p).is_file():
            spec = importlib.util.spec_from_file_location("kit_score", p)
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    raise SystemExit("cannot find kit score.py (set BR_SCORE_PY)")


def _load_sample(sample_dir: Path):
    tests = json.loads((sample_dir / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((sample_dir / "answers.json").read_text("utf-8"))["commits"]
    return tests, answers


def _ms(score, seconds: float) -> int:
    return score._duration_milliseconds(seconds)


def _cost_of(workers, dur_ms) -> int:
    occ = [w for w in workers if w]
    if not occ:
        return 0
    return max(sum(dur_ms[t] for t in w) for w in occ) + 2000 * len(occ)


def oracle_cost_ms(failing, dur_ms) -> int:
    if not failing:
        return 0
    return _cost_of(pack.best([(t, dur_ms[t]) for t in failing]), dur_ms)


def oracle_total(sample_dir: str | Path = "/kit/sample") -> float:
    """Sum over commits of the optimal cost of running exactly the failing tests, in seconds."""
    score = _load_score()
    tests, answers = _load_sample(Path(sample_dir))
    dur = {t["number"]: _ms(score, t["seconds"]) for t in tests}
    return sum(oracle_cost_ms(a["failing"], dur) for a in answers.values()) / 1000


def _fmt(ms: int) -> str:
    return f"{ms // 1000}.{ms % 1000:03d}"


def evaluate(output: Path, sample_dir: Path, cpu: float | None = None, quiet: bool = False) -> int:
    score = _load_score()
    tests, answers = _load_sample(sample_dir)
    raw = output.read_bytes()
    ev = score.evaluate_run(raw, tests, answers)
    if not ev["ok"]:
        print(f"Rejected: {ev['code']}" + (f" on line {ev['line']}" if ev["line"] is not None else ""))
        return 1
    dur = {t["number"]: _ms(score, t["seconds"]) for t in tests}
    chosen: dict[str, list[list[int]]] = {}
    for line in score._split_lines(raw.decode("utf-8")):
        v = json.loads(line)
        chosen[v["commit"]] = v["workers"]
    missed = set(ev["missed_commits"])

    rows = []
    tot_ours = tot_oracle = 0
    for c in sorted(answers):
        ws = chosen[c]
        sel = {t for w in ws for t in w}
        ours = _cost_of(ws, dur)
        orc = oracle_cost_ms(answers[c]["failing"], dur)
        miss = c in missed
        tot_ours += ours + (300_000 if miss else 0)
        tot_oracle += orc
        rows.append((c, answers[c].get("kind", "?"), ours, orc, len(sel), len(answers[c]["failing"]), miss))
    if not quiet:
        print(f"{'commit':8} {'kind':9} {'ours':>9} {'oracle':>9} {'excess':>9} {'sel':>5} {'fail':>5}  miss")
        for c, kind, ours, orc, ns, nf, miss in rows:
            print(
                f"{c:8} {kind:9} {_fmt(ours):>9} {_fmt(orc):>9} {_fmt(ours - orc):>9} "
                f"{ns:>5} {nf:>5}  {'MISS' if miss else ''}"
            )
        print(f"{'TOTAL':8} {'':9} {_fmt(tot_ours):>9} {_fmt(tot_oracle):>9} {_fmt(tot_ours - tot_oracle):>9}"
              "   (ours includes +300 s per miss)")
    sim = ev["simulated_milliseconds"]
    assert sim == tot_ours, (sim, tot_ours)
    print(f"Simulated: {_fmt(sim)} s")
    print(f"Misses:    {len(missed)}" + (" (" + ", ".join(sorted(missed)) + ")" if missed else ""))
    print(f"Oracle:    {_fmt(tot_oracle)} s")
    if cpu is not None:
        print(f"CPU:       {cpu:.6f} s")
        print(f"Total:     {sim / 1000 + cpu:.6f} s")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output", type=Path, nargs="?", help="our output.jsonl")
    ap.add_argument("--sample", type=Path, default=Path("/kit/sample"))
    ap.add_argument("--cpu", type=float, default=None, help="CPU seconds to add to the total")
    ap.add_argument("--oracle-only", action="store_true", help="just print the oracle total")
    ap.add_argument("--quiet", action="store_true", help="no per-commit table")
    a = ap.parse_args(argv)
    if a.oracle_only:
        print(f"{oracle_total(a.sample):.3f}")
        return 0
    if a.output is None:
        ap.error("output file required")
    return evaluate(a.output, a.sample, a.cpu, a.quiet)


if __name__ == "__main__":
    sys.exit(main())
