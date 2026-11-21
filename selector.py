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


def select(analyzer: Analyzer, patch_text: str, everything: set[int], modules: list[str],
           checker: ImportChecker) -> set[int]:
    chosen: set[int] = set()
    analyzer.import_time = {}
    for file_diff in parse_patch(patch_text):
        got = analyzer.tests_for(file_diff)
        if got is ALL:
            return set(everything)
        chosen |= got
    if analyzer.import_time and checker.crashes(analyzer.import_time):
        log("  import-time crash detected: selecting every test")
        return set(everything)
    return chosen & everything


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
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            chosen = select(analyzer, patch.read_text(encoding="utf-8", errors="replace"), everything, modules, checker)
        except Exception as error:  # noqa: BLE001 - a crash must never cost a miss
            log(f"{patch.stem}: fallback to all tests ({type(error).__name__}: {error})")
            chosen = set(everything)
        print(json.dumps({"commit": patch.stem, "workers": split(chosen, ms_of)}))
        log(f"{patch.stem}: {len(chosen)} tests")
    checker.close()
    sys.stdout.flush()


if __name__ == "__main__":
    main()
