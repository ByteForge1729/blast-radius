"""Blast Radius selector: pick the tests each sqlglot patch can break.

Reads /data/tests.json and /data/commits/*.patch, prints one JSON line per commit.
Coverage and fixture maps were built offline from sqlglot at the pinned commit
(see tools/). Any error on a commit falls back to running every test.
"""

from __future__ import annotations

import gzip
import json
import os
import pickle
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from br.diff import parse_patch  # noqa: E402
from br.impact import ALL, Analyzer, Maps  # noqa: E402
from br.importcheck import ImportChecker  # noqa: E402
from br import probe  # noqa: E402
from br.schedule import split  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")
MAPS = Path(os.environ.get("BR_MAPS", str(HERE / "maps")))


def log(message: str) -> None:
    print(message, file=sys.stderr)


def _load(name: str):
    """Load maps/<name> (.json or .json.gz), preferring a .pickle made at image build."""
    stem = name.removesuffix(".gz").removesuffix(".json")
    pickled = MAPS / f"{stem}.pickle"
    if pickled.exists():
        with open(pickled, "rb") as handle:
            return pickle.load(handle)
    path = MAPS / name
    if not path.exists():
        return None
    opener = gzip.open if name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def load_maps(tests: list[dict]) -> Maps:
    raw = _load("coverage.json.gz")
    if (MAPS / "coverage.pickle").exists():
        coverage = raw  # already int-keyed
    else:
        coverage = {path: {int(k): v for k, v in lines.items()} for path, lines in raw.items()}
    by_file: dict[str, set[int]] = defaultdict(set)
    for test in tests:
        by_file[test["file"]].add(test["number"])
    return Maps(
        coverage=coverage,
        fixtures=_load("fixtures.json") or {},
        tests_by_file=dict(by_file),
        classes=_load("classes.json.gz") or {},
        enums=frozenset(_load("enums.json") or ()),
        import_lines=_load("import_lines.json") or {},
        import_groups=_load("import_groups.json.gz") or {},
    )


MAX_PROBE_QUERIES = 30


class TpcdsProbe:
    """Decides test_execute_tpcds by running only the queries a function edit can reach."""

    def __init__(self, tests: list[dict], src_root: str) -> None:
        raw = _load("tpcds_queries.json.gz") or {}
        self.lines: dict[str, dict] = raw.get("lines", {})
        self.src_root = src_root
        self.number = next((t["number"] for t in tests if t["id"] == probe.TEST_ID), None)

    def queries(self, ranges: list[tuple[str, int, int]]) -> set[int]:
        out: set[int] = set()
        for path, start, end in ranges:
            lines = self.lines.get(path, {})
            for line in range(start, end + 1):
                out.update(lines.get(line, lines.get(str(line), ())))
        return out

    def prune(self, analyzer: Analyzer, chosen: set[int]) -> set[int]:
        if self.number not in chosen or not self.lines or analyzer.import_time:
            return chosen
        ranges = analyzer.changed_ranges
        if not ranges:
            return chosen
        queries = self.queries(ranges)
        if not queries or len(queries) > MAX_PROBE_QUERIES:
            log(f"  tpcds probe skipped: {len(queries)} queries")
            return chosen
        passed = probe.run_queries(self.src_root, analyzer.patched_sources, sorted(queries))
        log(f"  tpcds probe on {len(queries)} queries: {'pass, dropping it' if passed else 'fail, keeping it'}")
        return chosen - {self.number} if passed else chosen


def select(analyzer: Analyzer, patch_text: str, everything: set[int], modules: list[str],
           checker: ImportChecker, tpcds: TpcdsProbe | None = None) -> set[int]:
    chosen = _select(analyzer, patch_text, everything, modules, checker)
    if tpcds is not None and chosen != everything:
        chosen = tpcds.prune(analyzer, chosen)
    return chosen


def _select(analyzer: Analyzer, patch_text: str, everything: set[int], modules: list[str],
            checker: ImportChecker) -> set[int]:
    analyzer.reset()
    per_file: list[tuple[str, set[int] | None]] = []
    for file_diff in parse_patch(patch_text):
        per_file.append((file_diff.path, analyzer.tests_for(file_diff)))
    conservative = _union(got for _, got in per_file)
    if not analyzer.import_time:
        return set(everything) if conservative is ALL else conservative & everything
    crashed, changed = checker.check(analyzer.import_time)
    if crashed:
        log("  import-time crash detected: selecting every test")
        return set(everything)
    refined = analyzer.refine(changed)
    others = _union(got for path, got in per_file if path not in analyzer.refine_info)
    if refined is not None and others is not ALL:
        log(f"  import-time state changed: {sorted(changed)[:6]}")
        return (refined | others) & everything
    return set(everything) if conservative is ALL else conservative & everything


def _union(parts) -> set[int] | None:
    out: set[int] = set()
    for part in parts:
        if part is ALL:
            return ALL
        out |= part
    return out


def collection_modules() -> list[str]:
    """Module names test files import at collection (from maps/collect_imports.json)."""
    path = MAPS / "collect_imports.json"
    if not path.exists():
        return []
    names = []
    for rel in json.loads(path.read_text()):
        name = rel[:-3].replace("/", ".")
        names.append(name[: -len(".__init__")] if name.endswith(".__init__") else name)
    return names


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms_of = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    everything = set(ms_of)
    analyzer = Analyzer(SRC, load_maps(tests))
    modules = collection_modules()
    checker = ImportChecker(SRC, modules, analyzer.maps.import_groups.get("dependents", {}))
    tpcds = TpcdsProbe(tests, SRC)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            chosen = select(analyzer, patch.read_text(encoding="utf-8", errors="replace"), everything, modules,
                            checker, tpcds)
        except Exception as error:  # noqa: BLE001 - a crash must never cost a miss
            log(f"{patch.stem}: fallback to all tests ({type(error).__name__}: {error})")
            chosen = set(everything)
        print(json.dumps({"commit": patch.stem, "workers": split(chosen, ms_of)}))
        log(f"{patch.stem}: {len(chosen)} tests")
    checker.close()
    sys.stdout.flush()


if __name__ == "__main__":
    main()
