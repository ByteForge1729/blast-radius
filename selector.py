"""Blast Radius selector: pick the tests each sqlglot patch can break.

Reads /data/tests.json and /data/commits/*.patch, prints one JSON line per commit.
Coverage and fixture maps were built offline from sqlglot at the pinned commit
(see tools/). Any error on a commit falls back to running every test.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from br.diff import parse_patch  # noqa: E402
from br.impact import ALL, Analyzer, Maps  # noqa: E402
from br.importcheck import import_crashes  # noqa: E402
from br.schedule import split  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")
MAPS = Path(os.environ.get("BR_MAPS", str(HERE / "maps")))


def log(message: str) -> None:
    print(message, file=sys.stderr)


def load_maps(tests: list[dict]) -> Maps:
    with gzip.open(MAPS / "coverage.json.gz", "rt", encoding="utf-8") as handle:
        raw = json.load(handle)
    coverage = {path: {int(k): v for k, v in lines.items()} for path, lines in raw.items()}
    with open(MAPS / "fixtures.json", encoding="utf-8") as handle:
        fixtures = json.load(handle)
    classes: dict[str, list[int]] = {}
    if (MAPS / "classes.json.gz").exists():
        with gzip.open(MAPS / "classes.json.gz", "rt", encoding="utf-8") as handle:
            classes = json.load(handle)
    enums: frozenset[str] = frozenset()
    if (MAPS / "enums.json").exists():
        enums = frozenset(json.loads((MAPS / "enums.json").read_text()))
    import_lines: dict[str, list[int]] = {}
    if (MAPS / "import_lines.json").exists():
        import_lines = json.loads((MAPS / "import_lines.json").read_text())
    by_file: dict[str, set[int]] = defaultdict(set)
    for test in tests:
        by_file[test["file"]].add(test["number"])
    return Maps(coverage=coverage, fixtures=fixtures, tests_by_file=dict(by_file), classes=classes, enums=enums,
                import_lines=import_lines)


def select(analyzer: Analyzer, patch_text: str, everything: set[int], modules: list[str]) -> set[int]:
    chosen: set[int] = set()
    analyzer.import_time = {}
    for file_diff in parse_patch(patch_text):
        got = analyzer.tests_for(file_diff)
        if got is ALL:
            return set(everything)
        chosen |= got
    if analyzer.import_time and import_crashes(SRC, analyzer.import_time, modules):
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
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            chosen = select(analyzer, patch.read_text(encoding="utf-8", errors="replace"), everything, modules)
        except Exception as error:  # noqa: BLE001 - a crash must never cost a miss
            log(f"{patch.stem}: fallback to all tests ({type(error).__name__}: {error})")
            chosen = set(everything)
        print(json.dumps({"commit": patch.stem, "workers": split(chosen, ms_of)}))
        log(f"{patch.stem}: {len(chosen)} tests")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
