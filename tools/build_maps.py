"""Build maps/coverage.json and maps/fixtures.json from offline runs at the pinned commit.

Inputs:
  argv[1]  sqlglot checkout (with .coverage from `pytest --cov-context=test`)
  argv[2]  tests.json (numbering)
  argv[3]  directory holding fixture-trace files fx.*.json
  argv[4]  output directory (maps/)

coverage.json: {"<file>": {"<line>": [test numbers...]}} for every line some test executed.
fixtures.json: {"<fixture path>": [test numbers...]}
"""

import glob
import json
import os
import sys
from collections import defaultdict

from coverage import CoverageData


def context_to_nodeid(context: str) -> str:
    return context.rsplit("|", 1)[0]


def build_coverage(repo: str, number_of: dict[str, int]) -> dict:
    data = CoverageData(basename=os.path.join(repo, ".coverage"))
    data.read()
    result: dict[str, dict[str, list[int]]] = {}
    unknown: set[str] = set()
    for path in data.measured_files():
        rel = os.path.relpath(path, repo).replace(os.sep, "/")
        by_line = data.contexts_by_lineno(path)
        lines: dict[str, list[int]] = {}
        for line, contexts in by_line.items():
            nums = set()
            for ctx in contexts:
                if not ctx:
                    continue
                nodeid = context_to_nodeid(ctx)
                if nodeid in number_of:
                    nums.add(number_of[nodeid])
                else:
                    unknown.add(nodeid)
            if nums:
                lines[str(line)] = sorted(nums)
        if lines:
            result[rel] = lines
    print(f"coverage: {len(result)} files, unknown contexts {len(unknown)}", file=sys.stderr)
    return result


def build_fixtures(trace_dir: str, number_of: dict[str, int]) -> dict:
    inverse: dict[str, set[int]] = defaultdict(set)
    for path in glob.glob(os.path.join(trace_dir, "fx.*.json")):
        with open(path, encoding="utf-8") as handle:
            for nodeid, files in json.load(handle).items():
                if nodeid in number_of:
                    for f in files:
                        inverse[f].add(number_of[nodeid])
    return {k: sorted(v) for k, v in sorted(inverse.items())}


def main() -> None:
    repo, tests_json, trace_dir, out_dir = sys.argv[1:5]
    with open(tests_json, encoding="utf-8") as handle:
        tests = json.load(handle)["tests"]
    number_of = {t["id"]: t["number"] for t in tests}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "coverage.json"), "w", encoding="utf-8") as handle:
        json.dump(build_coverage(repo, number_of), handle, separators=(",", ":"))
    with open(os.path.join(out_dir, "fixtures.json"), "w", encoding="utf-8") as handle:
        json.dump(build_fixtures(trace_dir, number_of), handle, indent=1)


if __name__ == "__main__":
    main()
