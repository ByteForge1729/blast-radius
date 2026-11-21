# blast-radius

Coverage-guided test selection for sqlglot patches, built for the RWX "Blast Radius" track of the Builders Buildathon 2026.

**Status:** working prototype, under active development during the hackathon.

## Overview

Each input is a patch against sqlglot at commit `eb11aec4`. The selector prints, per patch, which tests to run and how to split them across up to 8 workers. Every missed failing test costs 300 s, and every selected test costs its runtime, so the goal is to pick the smallest set that is still safe.

## How it works

1. **Maps built once, offline, from the pinned commit** (`tools/`):
   - `maps/coverage.json.gz`: for every source line, the tests that executed it (pytest-cov with per-test contexts). Process pools in the test suite are run inline, with pickling preserved, so their work is attributed to the right test.
   - `maps/fixtures.json`: which tests open which files under `tests/fixtures/`.
2. **Per patch, at run time** (`br/`):
   - Each hunk is applied on its own and both versions are parsed. If the syntax tree is unchanged (comments, blank lines), the hunk is ignored.
   - A changed statement inside a function or lambda selects the tests that executed any line of that statement.
   - Import-time changes (class or module level, new methods, signatures) fall back to the tests that touched that class, module or dialect family.
   - Fixture edits select the tests that read the fixture. SQL comment-only fixture edits select nothing.
   - Syntax errors, deleted modules and unknown files select every test. Any internal error falls back to every test.
3. **Scheduling** (`br/schedule.py`): longest-first packing, trying 1 to 8 workers and keeping the cheapest (busiest worker + 2 s per worker).

## Layout

| Path | Purpose |
| --- | --- |
| `selector.py` | Entry point run by the image |
| `br/diff.py` | Unified diff parsing and hunk application |
| `br/impact.py` | Patch to affected tests |
| `br/schedule.py` | Worker split |
| `maps/` | Coverage and fixture maps from the pinned commit |
| `tools/` | Scripts that rebuild the maps |
| `sample/`, `score.py`, `SCORING.md` | Organiser's sample set and scorer |

## Usage

```sh
make sample      # needs Docker: build, run on the 30 sample commits, score
```

Without Docker, against a local sqlglot checkout:

```sh
BR_DATA=sample BR_SQLGLOT=path/to/sqlglot python selector.py > output.jsonl
python score.py sample sample/answers.json output.jsonl
```

Rebuilding the maps (sqlglot checkout at `eb11aec4`, `requirements.lock` plus `pytest-cov` installed):

```sh
cd path/to/sqlglot
INLINE_POOLS=1 SKIP_INTEGRATION=0 FIXTURE_TRACE_OUT=../trace/fx PYTHONPATH=.:../../tools \
  python -m pytest -p fixture_tracer -n 8 --cov=sqlglot --cov=tests --cov-context=test --cov-report=
python tools/build_maps.py path/to/sqlglot sample/tests.json ../trace maps && gzip -9 -f maps/coverage.json
```

## Results

Local `score.py` on the organiser's 30 sample commits (simulated seconds, CPU not included):

| Version | Simulated seconds | Misses |
| --- | --- | --- |
| Starter (run everything on 3 workers) | 1210.5 | 0 |
| Oracle (exactly the failing tests, best packing) | 94.4 | 0 |
| v1 line coverage + AST no-op detection | 178.3 | 0 |
| v3 + class-setup attribution, class-usage tracing | 177.0 | 0 |
| v4 + `is`/`==` equivalence for None and identity enums | 126.8 | 0 |

Practice board (hidden commits, middle of 3 runs): v1 scored 497.0 s (195.6 s plus one missed commit; 1.4 s CPU).

Mutation testing (`tools/mutate.py`, random operator, constant, boolean, comment and fixture edits, every unselected test run for real): 120 mutants so far; the one miss found (a dialect class constant) is fixed in v3.

## Limitations

- Coverage is line-level. Class-level edits rely on class-usage tracing (which tests constructed the class or a subclass); a class whose attributes are read without ever being instantiated falls back to module or dialect-family coverage.
- The `is`/`==` equivalence assumes no object with an unusual `__eq__` reaches the changed comparison; only the executor's sort wrapper is known to have one, and its file is excluded.
- Maps were built on macOS arm64 with Python 3.13. The scorer runs linux/amd64, where a few platform-dependent paths might differ.
- The Docker image has not been built locally (no Docker on the dev machine).

## License

MIT, see [LICENSE](LICENSE).
