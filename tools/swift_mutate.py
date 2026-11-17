#!/usr/bin/env python3
"""Operator-based mutation testing for the Swift package (todone/).

tools/mutate.py runs hand-written mutants against the Python suite. This one
writes its own: it reads the Swift sources, finds every place a standard
mutation operator applies (outside comments and string literals), and for
each one changes that single spot, rebuilds, and runs `swift test`.

    KILLED     a test failed (or the run timed out): the suite catches it
    SURVIVED   every test passed: nothing checks that exact behavior
    UNVIABLE   the mutant didn't compile; it says nothing about the tests

Operators: relational boundaries (< <=, > >=), equality (== !=), logic
(&& ||), arithmetic (+ -), compound assignment (+= -=), true/false, dropping
a prefix `!`, .first/.last, min/max, and the integer literals 0 and 1.
Binary operators are only matched with a space on each side, which is how
this codebase writes them, so generics (`Array<T>`) and arrows aren't touched.

It edits the package's files IN PLACE and restores each one after its run
(also on Ctrl-C). Run it in a separate worktree, not in a checkout you're
editing; a full run takes a while (see `--list` for the count first).

    .venv/bin/python tools/swift_mutate.py --list
    .venv/bin/python tools/swift_mutate.py --since HEAD~1          # only changed lines
    .venv/bin/python tools/swift_mutate.py --filter QuickAdd --report out.json
    .venv/bin/python tools/swift_mutate.py --rerun out.json             # just its survivors

Exits non-zero if any mutant survives.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PACKAGE = REPO / "todone"
DEFAULT_SOURCES = "Sources/TodoneCore"


# ---------------------------------------------------------------------------
# Finding code (as opposed to comments and strings)
# ---------------------------------------------------------------------------


def code_mask(src: str) -> list[bool]:
    """True for each character of `src` that is code, False inside comments and
    string literals (plain, multi-line, raw `#"..."#`; interpolations count as
    string). Nested block comments are handled, as Swift allows them."""
    mask = [True] * len(src)
    i, n = 0, len(src)
    while i < n:
        end = _literal_end(src, i)
        if end is None:
            i += 1
            continue
        for k in range(i, end):
            mask[k] = False
        i = end
    return mask


def _literal_end(src: str, i: int) -> int | None:
    """If a comment or string starts at `i`, the index just past it; else None."""
    if src.startswith("//", i):
        newline = src.find("\n", i)
        return len(src) if newline < 0 else newline
    if src.startswith("/*", i):
        return _block_comment_end(src, i)
    hashes = len(src) - len(src[i:].lstrip("#")) - i if src[i] == "#" else 0
    j = i + hashes
    if j < len(src) and src[j] == '"':
        return _string_end(src, j, hashes)
    return None


def _block_comment_end(src: str, i: int) -> int:
    depth, j = 0, i
    while j < len(src):
        if src.startswith("/*", j):
            depth, j = depth + 1, j + 2
        elif src.startswith("*/", j):
            depth, j = depth - 1, j + 2
            if depth == 0:
                return j
        else:
            j += 1
    return len(src)


def _string_end(src: str, quote: int, hashes: int) -> int:
    """Past the closing delimiter of the string whose first quote is at `quote`."""
    delim = '"""' if src.startswith('"""', quote) else '"'
    close = delim + "#" * hashes
    j = quote + len(delim)
    while j < len(src):
        # Escapes only count in a non-raw string (a raw one escapes with \#).
        if src[j] == "\\" and hashes == 0:
            j += 2
            continue
        if src.startswith(close, j):
            return j + len(close)
        if delim == '"' and src[j] == "\n":
            return j  # unterminated single-line string: stop at the line end
        j += 1
    return len(src)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Mutant:
    file: str
    line: int
    column: int
    start: int  # character offset in the file
    original: str
    replacement: str
    operator: str

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line}:{self.column}"

    def apply(self, src: str) -> str:
        assert src[self.start:self.start + len(self.original)] == self.original
        return src[:self.start] + self.replacement + src[self.start + len(self.original):]


# (operator name, regex, group to replace, replacement)
_BINARY = [
    ("boundary", " <= ", " < "), ("boundary", " < ", " <= "),
    ("boundary", " >= ", " > "), ("boundary", " > ", " >= "),
    ("equality", " == ", " != "), ("equality", " != ", " == "),
    ("logic", " && ", " || "), ("logic", " || ", " && "),
    ("arithmetic", " + ", " - "), ("arithmetic", " - ", " + "),
    ("assignment", " += ", " -= "), ("assignment", " -= ", " += "),
]
_WORDS = [
    ("boolean", r"\btrue\b", "false"), ("boolean", r"\bfalse\b", "true"),
    ("first/last", r"(?<=\.)first\b", "last"), ("first/last", r"(?<=\.)last\b", "first"),
    ("min/max", r"\bmin(?=\()", "max"), ("min/max", r"\bmax(?=\()", "min"),
    ("literal", r"(?<![\w.])0(?![\w.])", "1"), ("literal", r"(?<![\w.])1(?![\w.])", "0"),
]
# A prefix `!`: not after an identifier, `)`, `]` or `?` (those are force unwraps,
# try!/as!), not `!=`, and followed by something it can negate.
_NOT = re.compile(r"(?<![\w)\]?!])!(?=[\w(.$])")


def find_mutants(src: str, file: str = "") -> list[Mutant]:
    """Every single-spot mutant of `src`, in file order."""
    mask = code_mask(src)
    found: list[tuple[int, str, str, str]] = []
    for name, old, new in _BINARY:
        start = src.find(old)
        while start >= 0:
            found.append((start, old, new, name))
            start = src.find(old, start + 1)
    for name, pattern, new in _WORDS:
        for m in re.finditer(pattern, src):
            found.append((m.start(), m.group(), new, name))
    for m in _NOT.finditer(src):
        found.append((m.start(), "!", "", "negation"))
    mutants = []
    for start, old, new, name in sorted(set(found)):
        if all(mask[start:start + len(old)]):
            line = src.count("\n", 0, start) + 1
            column = start - (src.rfind("\n", 0, start) + 1) + 1
            mutants.append(Mutant(file, line, column, start, old, new, name))
    return mutants


# ---------------------------------------------------------------------------
# Choosing what to mutate
# ---------------------------------------------------------------------------


def changed_lines(diff: str) -> dict[str, set[int]]:
    """Lines added or changed per file, from `git diff -U0` output (new-side numbers)."""
    lines: dict[str, set[int]] = {}
    current = None
    for row in diff.splitlines():
        if row.startswith("+++ "):
            path = row[4:]
            current = None if path == "/dev/null" else path.removeprefix("b/")
        elif row.startswith("@@") and current:
            m = re.search(r"\+(\d+)(?:,(\d+))?", row)
            start, count = int(m.group(1)), int(m.group(2) or "1")
            lines.setdefault(current, set()).update(range(start, start + count))
    return lines


def collect(package: Path, sources: str, since: str | None, needle: str | None) -> list[Mutant]:
    files = sorted((package / sources).rglob("*.swift"))
    keep = None
    if since:
        diff = subprocess.run(["git", "diff", "-U0", since, "--", str(package / sources)],
                              cwd=package, capture_output=True, text=True, check=True).stdout
        top = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=package,
                                  capture_output=True, text=True, check=True).stdout.strip())
        keep = {(top / f).resolve(): ls for f, ls in changed_lines(diff).items()}
    mutants = []
    for path in files:
        rel = str(path.relative_to(package))
        if needle and needle.lower() not in rel.lower():
            continue
        wanted = None if keep is None else keep.get(path.resolve(), set())
        mutants += [m for m in find_mutants(path.read_text(), rel) if wanted is None or m.line in wanted]
    return mutants


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def build(package: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["swift", "build", "--build-tests"], cwd=package, capture_output=True, text=True)


def test(package: Path, timeout: float) -> subprocess.CompletedProcess | None:
    """The suite's result, or None when it ran past `timeout` (counted as killed)."""
    try:
        return subprocess.run(["swift", "test", "--skip-build"], cwd=package,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def run_mutant(package: Path, mutant: Mutant, timeout: float) -> str:
    path = package / mutant.file
    original = path.read_text()
    try:
        path.write_text(mutant.apply(original))
        if build(package).returncode != 0:
            return "UNVIABLE"
        result = test(package, timeout)
        return "KILLED" if result is None or result.returncode != 0 else "SURVIVED"
    finally:
        path.write_text(original)


def survivors_of(report: Path, mutants: list[Mutant]) -> list[Mutant]:
    """The mutants a previous --report run left SURVIVED and that still apply as-is
    (same file, offset and text); one whose code has since changed drops out."""
    keys = {(r["file"], r["start"], r["original"], r["replacement"])
            for r in json.loads(report.read_text()) if r["outcome"] == "SURVIVED"}
    return [m for m in mutants if (m.file, m.start, m.original, m.replacement) in keys]


def baseline_timeout(package: Path) -> float | None:
    """Build and test the unmutated package; the per-mutant timeout (ten times the
    baseline, at least a minute), or None when the baseline already fails, since
    then every mutant would "die" for the wrong reason."""
    if build(package).returncode != 0:
        print("Baseline build fails — fix that first.", file=sys.stderr)
        return None
    started = time.monotonic()
    result = test(package, timeout=600)
    if result is None or result.returncode != 0:
        print("Baseline suite fails before any mutation — fix that first.", file=sys.stderr)
        return None
    return max(60.0, 10 * (time.monotonic() - started))


def print_list(mutants: list[Mutant]) -> None:
    for m in mutants:
        print(f"{m.where}  {m.operator:<11} {m.original.strip()!r} -> {m.replacement.strip()!r}")
    print(f"{len(mutants)} mutants")


def summarize(results: list[dict]) -> int:
    """Print the score and survivors; the exit code (1 if anything survived)."""
    counts = {k: sum(r["outcome"] == k for r in results) for k in ("KILLED", "SURVIVED", "UNVIABLE")}
    viable = counts["KILLED"] + counts["SURVIVED"]
    score = 100 * counts["KILLED"] / viable if viable else 100.0
    print(f"\n{counts['KILLED']}/{viable} viable mutants killed ({score:.1f}%), "
          f"{counts['UNVIABLE']} didn't compile")
    for r in results:
        if r["outcome"] == "SURVIVED":
            print(f"  survivor: {r['file']}:{r['line']}:{r['column']}  {r['operator']}: "
                  f"{r['original'].strip()!r} -> {r['replacement'].strip()!r}")
    return 1 if counts["SURVIVED"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE, help="the Swift package to mutate")
    parser.add_argument("--sources", default=DEFAULT_SOURCES, help="source directory inside the package")
    parser.add_argument("--since", help="only mutate lines changed since this git revision")
    parser.add_argument("--filter", help="only files whose path contains this")
    parser.add_argument("--list", action="store_true", help="print the mutants and stop")
    parser.add_argument("--report", type=Path, help="write every result as JSON here")
    parser.add_argument("--rerun", type=Path, help="only the survivors in this earlier --report")
    args = parser.parse_args(argv)

    package = args.package.resolve()
    mutants = collect(package, args.sources, args.since, args.filter)
    if args.rerun:
        mutants = survivors_of(args.rerun, mutants)
    if args.list:
        print_list(mutants)
        return 0
    if not mutants:
        print("No mutants to run.", file=sys.stderr)
        return 1
    timeout = baseline_timeout(package)
    if timeout is None:
        return 1

    results = []
    for index, mutant in enumerate(mutants, 1):
        outcome = run_mutant(package, mutant, timeout)
        results.append({**asdict(mutant), "outcome": outcome})
        print(f"[{index}/{len(mutants)}] {outcome:<9} {mutant.where}  "
              f"{mutant.original.strip()!r} -> {mutant.replacement.strip()!r}", flush=True)
    if args.report:
        args.report.write_text(json.dumps(results, indent=1))
    return summarize(results)


if __name__ == "__main__":
    sys.exit(main())
