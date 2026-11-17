#!/usr/bin/env python3
"""Targeted mutation testing: does the suite actually catch a defect?

Running the tests only proves they pass. This proves they'd fail: each mutant
is a small, deliberate behavior change applied to a throwaway copy of the repo.
A mutant that KILLS (the suite fails) means the tests cover that behavior; one
that SURVIVES is a gap — the code could be wrong in that exact way and nothing
would notice.

This is deliberately not a whole-repo mutation run (mutmut/cosmic-ray do that,
and take minutes). You hand-write a handful of mutants aimed at the logic you
just changed — boundaries, dropped guards, flipped defaults — and get an answer
in seconds. Add mutants to tools/mutants.toml as you write new logic; the ones
already there stay as a regression check that the suite hasn't rotted.

    .venv/bin/python tools/mutate.py
    .venv/bin/python tools/mutate.py --filter park     # just the parking ones

Exits non-zero if any mutant survives.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SPEC = REPO / "tools" / "mutants.toml"
IGNORE = shutil.ignore_patterns(
    ".venv", ".git", "__pycache__", ".pytest_cache", ".hypothesis", "*.egg-info",
    # The Swift build output and app bundle: large, and no Python test reads them.
    ".build", "Todone.app",
)


def load_mutants(spec: Path) -> list[dict]:
    with spec.open("rb") as f:
        data = tomllib.load(f)
    mutants = data.get("mutant", [])
    for m in mutants:
        missing = {"file", "old", "new", "label"} - set(m)
        if missing:
            raise ValueError(f"mutant {m.get('label', '?')!r} is missing {sorted(missing)}")
    return mutants


def run_suite(work: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "-x"],
        cwd=work, capture_output=True, text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC, help="mutant definitions (TOML)")
    parser.add_argument("--filter", help="only run mutants whose label or file contains this")
    args = parser.parse_args()

    mutants = load_mutants(args.spec)
    if args.filter:
        needle = args.filter.lower()
        mutants = [m for m in mutants if needle in m["label"].lower() or needle in m["file"].lower()]
    if not mutants:
        print("No mutants to run.", file=sys.stderr)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="mutate-"))
    work = tmp / "repo"
    try:
        # Baseline first: if the unmutated copy already fails, every mutant
        # would "die" for the wrong reason and the whole run means nothing.
        shutil.copytree(REPO, work, ignore=IGNORE)
        baseline = run_suite(work)
        if baseline.returncode != 0:
            print("Baseline suite FAILS before any mutation — fix that first:\n", file=sys.stderr)
            print(baseline.stdout[-2000:], file=sys.stderr)
            return 1

        survived: list[dict] = []
        for mutant in mutants:
            shutil.rmtree(work)
            shutil.copytree(REPO, work, ignore=IGNORE)
            path = work / mutant["file"]
            text = path.read_text()
            hits = text.count(mutant["old"])
            if hits != 1:
                print(f"SKIPPED   {mutant['file']}: {mutant['label']} "
                      f"(anchor matched {hits} times, expected 1 — stale mutant?)")
                survived.append(mutant)
                continue
            path.write_text(text.replace(mutant["old"], mutant["new"]))

            killed = run_suite(work).returncode != 0
            print(f"{'KILLED  ' if killed else 'SURVIVED'}  {mutant['file']}: {mutant['label']}")
            if not killed:
                survived.append(mutant)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(mutants) - len(survived)}/{len(mutants)} killed")
    for mutant in survived:
        print(f"  survivor: {mutant['file']}: {mutant['label']}")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
