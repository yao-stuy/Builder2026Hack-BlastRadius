# Blast Radius

Test selection for changes to [sqlglot](https://github.com/tobymao/sqlglot) at commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. For every patch in `/data/commits` the program prints which
tests to run and how to split them over workers.

## Approach

The program never looks a patch up in a table. It works out what a change can affect from maps of the
pinned commit, and then, where that is cheap, runs the candidate tests against the patched sources to see
what really fails.

1. **What changed.** The diff is parsed and applied in memory (`br/diffparse.py`). Each changed Python
   file is classified (`br/classify.py`): no semantic change (comments, blank lines, formatting),
   statements inside a function body, statements executed at import time (class or module level), a file
   that no longer compiles, or a change we cannot localize.
2. **Which tests could be affected** (`br/select.py`, maps in `maps/`):
   - *Line coverage.* Every test was recorded once at the pinned commit, each in a fresh process, so a
     test's line set contains everything it depends on, including import-time lines of every module it
     loads. A change inside a function can only affect tests that execute the changed statement.
   - *File reads.* Tests that open a changed fixture or data file.
   - *Class usage.* A statement in a class body runs for every importer, but it can only matter to tests
     that use the class: tests that created an instance of it, of a subclass, or of a class of the same
     dialect, or that ran code in the class body.
   - *Constant references.* For a plain `NAME = value` assignment, only tests that run code mentioning
     `NAME`.
   - Anything else (module-level edits we cannot narrow, renamed or deleted files, unparseable input)
     falls back to every test that loads the file, and finally to the whole suite.
3. **Verification** (`br/engine.py`, `br/harness.py`, `br/overlay.py`). When the estimated CPU cost is lower
   than what the candidates would cost to emit, the patch is applied to a symlink mirror of the sources and
   the candidates run in a forked child with a small `unittest` runner (same outcomes as pytest: failures,
   errors, failing subtests, import errors, timeouts). Tests that pass are dropped.
   - Loop-style tests (fixture-driven optimizer and executor tests) run only the fixture entries that
     execute a changed line, or that were themselves changed (`br/slices.py`, `br/entrymaps.py`).
   - The slowest tests are never run in full. `test_max_nodes` is checked with a scaled-down copy of its
     own body, `test_generator_memory` only for the dialects whose code changed, and the TPC tables of
     `TestExecutor` are loaded lazily (`br/adapters.py`). A loop test whose affected entries are too many
     to run is sampled; it is dropped only if its sample and every other candidate pass.
4. **Output.** Whatever failed, plus every candidate that was not verified, packed over 1 to 8 workers to
   minimize the busiest worker plus the per-worker cost (`br/pack.py`).

Any internal error, timeout, or doubt widens the answer; the last resort is the full suite for that commit.

## Maps

Everything under `maps/` is generated from the pinned commit only, by the scripts in `tools/`:

| File | Content | Built by |
| --- | --- | --- |
| `cov.bin`, `index.json`, `meta.json` | per-test line coverage, timings | `tools/record.py`, `tools/buildmaps.py` |
| `reads.json.z` | files each test opens | same |
| `classes.json.z`, `uses.bin` | class table and which tests instantiate which classes | `tools/classhook.py`, `tools/classtable.py` |
| `refs.json.z` | where constant-style names are read | `tools/build_refs.py` |
| `entries.bin`, `entries.idx.json` | per-fixture-entry coverage of loop tests | `tools/record_entries.py` |
| `adapters.json`, `adapters.json.z` | source hashes and per-dialect coverage for the stand-ins | `tools/record_adapters.py` |

`tools/mutgen.py`, `tools/recall.py`, `tools/eval.py` and `tools/labelplan.py` are for local validation on
synthetic patches; none of their output is shipped or read at run time.

## Layout

```
Dockerfile            pinned base image, pinned packages, sqlglot at the pinned commit
br/                   run-time code (standard library only)
maps/                 maps of the pinned commit
tools/                map builders and local validation tools
tests_br/             unit tests for the run-time code
```
