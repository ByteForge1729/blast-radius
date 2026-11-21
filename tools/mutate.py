"""Mutation harness: find patches where the selector misses a failing test.

For each random mutant (operator flips, constants, comments, fixture edits) it writes a
unified diff, asks the selector which tests to run, then runs every *unselected* test on
a scratch copy of sqlglot. Any failure there is a miss worth 300 s on the real board.

Usage (from the blast-radius folder):
  python tools/mutate.py --count 40 --seed 1
Writes work/mutants/<id>.patch and appends a line per mutant to work/mutants/results.jsonl.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from br.diff import parse_patch  # noqa: E402
from br.impact import ALL, Analyzer  # noqa: E402
from selector import load_maps  # noqa: E402

PRISTINE = ROOT / "work" / "sqlglot"
SCRATCH = ROOT / "work" / "mutsrc"
OUT = ROOT / "work" / "mutants"
PYTHON = ROOT / "work" / ".venv" / "bin" / "python"
SWAPS = {
    ast.And: ast.Or, ast.Or: ast.And, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.Eq,
    ast.IsNot: ast.NotEq, ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}
OP_TEXT = {ast.And: "and", ast.Or: "or", ast.Eq: "==", ast.NotEq: "!=", ast.Is: "is",
           ast.IsNot: "is not", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
           ast.In: "in", ast.NotIn: "not in"}


def _replace_on_line(line: str, old: str, new: str, rng: random.Random) -> str | None:
    spots = [i for i in range(len(line)) if line.startswith(f" {old} ", i)]
    if not spots:
        return None
    i = rng.choice(spots)
    return line[:i] + f" {new} " + line[i + len(old) + 2:]


def mutate_python(text: str, rng: random.Random) -> tuple[str, str] | None:
    lines = text.split("\n")
    tree = ast.parse(text)
    kind = rng.choice(["op", "op", "op", "const", "bool", "comment", "not"])
    nodes = list(ast.walk(tree))
    rng.shuffle(nodes)
    for node in nodes:
        lineno = getattr(node, "lineno", None)
        if not lineno or getattr(node, "end_lineno", lineno) != lineno and kind != "comment":
            continue
        line = lines[lineno - 1]
        new = None
        if kind == "op" and isinstance(node, ast.BoolOp):
            new = _replace_on_line(line, OP_TEXT[type(node.op)], OP_TEXT[SWAPS[type(node.op)]], rng)
        elif kind == "op" and isinstance(node, ast.Compare):
            op = type(node.ops[0])
            new = _replace_on_line(line, OP_TEXT[op], OP_TEXT[SWAPS[op]], rng) if op in SWAPS else None
        elif kind == "const" and isinstance(node, ast.Constant) and type(node.value) is int:
            seg = ast.get_source_segment(text, node)
            if seg and seg.isdigit():
                start = node.col_offset
                new = line[:start] + str(node.value + rng.choice([-1, 1])) + line[start + len(seg):]
        elif kind == "bool" and isinstance(node, ast.Constant) and type(node.value) is bool:
            seg = ast.get_source_segment(text, node)
            start = node.col_offset
            new = line[:start] + str(not node.value) + line[start + len(seg):]
        elif kind == "not" and isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            new = line[:node.col_offset] + line[node.col_offset + 4:]
        elif kind == "comment" and isinstance(node, ast.stmt):
            indent = line[: len(line) - len(line.lstrip())]
            mutated = lines[: lineno - 1] + [indent + "# Cheap check first"] + lines[lineno - 1:]
            return kind, "\n".join(mutated)
        if new and new != line:
            return kind, "\n".join(lines[: lineno - 1] + [new] + lines[lineno:])
    return None


def mutate_fixture(text: str, rng: random.Random) -> tuple[str, str] | None:
    lines = text.split("\n")
    candidates = [i for i, l in enumerate(lines) if l.strip() and not l.startswith(("--", "#"))]
    if not candidates:
        return None
    i = rng.choice(candidates)
    words = lines[i].split(" ")
    j = rng.randrange(len(words))
    words[j] = words[j][::-1] if len(words[j]) > 1 else words[j] + "x"
    return "fixture", "\n".join(lines[:i] + [" ".join(words)] + lines[i + 1:])


def make_patch(rel: str, old: str, new: str) -> str:
    diff = difflib.unified_diff(old.split("\n"), new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    return f"diff --git a/{rel} b/{rel}\n" + "\n".join(diff) + "\n"


def pick_target(rng: random.Random) -> Path:
    if rng.random() < 0.3:
        return rng.choice(sorted((PRISTINE / "tests" / "fixtures").rglob("*.sql")))
    return rng.choice(sorted(p for p in (PRISTINE / "sqlglot").rglob("*.py") if "__pycache__" not in str(p)))


def run_unselected(unselected_ids: list[str]) -> list[str]:
    if not unselected_ids:
        return []
    cmd = [str(PYTHON), "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "8", "--timeout", "120",
           "-rfE", *unselected_ids]
    env = {"PATH": f"{PYTHON.parent}:/usr/bin:/bin","SKIP_INTEGRATION": "0", "PYTHONPATH": str(SCRATCH), "HOME": str(SCRATCH)}
    proc = subprocess.run(cmd, cwd=SCRATCH, capture_output=True, text=True, env=env, timeout=900)
    failed = []
    for line in proc.stdout.splitlines():
        if line.startswith(("FAILED ", "ERROR ")):
            failed.append(line.split(" ", 1)[1].split(" - ", 1)[0].strip())
    if proc.returncode not in (0, 1) and not failed:
        failed.append(f"<pytest exit {proc.returncode}>")
    return failed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    tests = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
    id_of = {t["number"]: t["id"] for t in tests}
    analyzer = Analyzer(str(PRISTINE), load_maps(tests))
    OUT.mkdir(parents=True, exist_ok=True)
    if not SCRATCH.exists():
        shutil.copytree(PRISTINE, SCRATCH, ignore=shutil.ignore_patterns(".git", ".coverage*", "__pycache__"))
    done = 0
    while done < args.count:
        target = pick_target(rng)
        rel = str(target.relative_to(PRISTINE))
        old = target.read_text(encoding="utf-8")
        mutated = (mutate_fixture if rel.endswith(".sql") else mutate_python)(old, rng)
        if mutated is None:
            continue
        kind, new = mutated
        if kind not in ("fixture", "comment"):
            try:
                ast.parse(new)
            except SyntaxError:
                continue
        patch = make_patch(rel, old, new)
        mid = f"s{args.seed}-{done:03d}"
        (OUT / f"{mid}.patch").write_text(patch)
        chosen: set[int] = set()
        for fd in parse_patch(patch):
            got = analyzer.tests_for(fd)
            chosen = set(id_of) if got is ALL else chosen | got
        scratch_file = SCRATCH / rel
        scratch_file.write_text(new, encoding="utf-8")
        try:
            missed = run_unselected([id_of[n] for n in sorted(set(id_of) - chosen)])
        finally:
            scratch_file.write_text(old, encoding="utf-8")
        record = {"id": mid, "kind": kind, "file": rel, "selected": len(chosen), "missed": missed}
        with open(OUT / "results.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        print(json.dumps(record), flush=True)
        done += 1


if __name__ == "__main__":
    main()
