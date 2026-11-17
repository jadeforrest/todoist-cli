"""Tests for tools/swift_mutate.py: what counts as code, which mutants it writes."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

_PATH = Path(__file__).resolve().parent.parent / "tools" / "swift_mutate.py"
_spec = importlib.util.spec_from_file_location("swift_mutate", _PATH)
sm = importlib.util.module_from_spec(_spec)
sys.modules["swift_mutate"] = sm  # dataclasses look their module up here
_spec.loader.exec_module(sm)


def code_text(src: str) -> str:
    return "".join(c if keep else "_" for c, keep in zip(src, sm.code_mask(src)))


def ops(src: str) -> list[tuple[str, str, str]]:
    return [(m.operator, m.original.strip(), m.replacement.strip()) for m in sm.find_mutants(src)]


class TestCodeMask:
    def test_line_and_block_comments(self):
        src = "a < b // x < y\nc /* d /* nested */ e */ f"
        assert code_text(src) == "a < b ________\nc ______________________ f"

    def test_strings(self):
        src = 'let s = "a == b \\" c" + t'
        assert code_text(src) == "let s = " + "_" * 13 + " + t"

    def test_raw_and_multiline_strings(self):
        src = 'x = #"a"b == c"# + """\nq == r\n""" + y'
        out = code_text(src)
        assert out.startswith("x = ") and out.endswith(" + y")
        assert "==" not in out

    def test_raw_string_backslash_is_not_an_escape(self):
        src = r'p = #"\d"# + 1'
        assert code_text(src) == "p = ______ + 1"

    def test_unterminated_string_stops_at_line_end(self):
        assert code_text('a = "oops\nb == c') == "a = _____\nb == c"

    def test_unterminated_block_comment_runs_to_end(self):
        assert code_text("a /* never closed") == "a " + "_" * 15

    def test_hash_without_quote_is_code(self):
        assert code_text("#if DEBUG\n#filePath") == "#if DEBUG\n#filePath"


class TestOperators:
    def test_binary_operators_need_spaces(self):
        assert ops("a <= b") == [("boundary", "<=", "<")]
        assert ops("Array<Int> -> x") == []
        assert ops("f(by: -1)") == [("literal", "1", "0")]
        assert set(ops("a == b && c != d")) == {
            ("equality", "==", "!="), ("logic", "&&", "||"), ("equality", "!=", "==")}

    def test_assignment_and_arithmetic(self):
        assert set(ops("x += y - z")) == {("assignment", "+=", "-="), ("arithmetic", "-", "+")}

    def test_words(self):
        assert set(ops("if true { list.first; min(a, b) }")) == {
            ("boolean", "true", "false"), ("first/last", "first", "last"), ("min/max", "min", "max")}
        assert ops("firstName lastly minimum(x) .lastIndex") == []

    def test_literals_only_whole_numbers(self):
        assert ops("x = 0") == [("literal", "0", "1")]
        assert ops("y = 10 + 0.5") == [("arithmetic", "+", "-")]
        assert ops("1...4") == []

    def test_negation_but_not_unwraps(self):
        assert ops("if !done {") == [("negation", "!", "")]
        assert ops("try! f(); x!.y; a as! B; c?!") == []
        assert ops("guard !(a) else") == [("negation", "!", "")]

    def test_nothing_inside_comments_or_strings(self):
        assert ops('// a == b\nlet s = "true"') == []

    def test_positions_and_apply(self):
        src = "let a = 1\nif b < c {}"
        boundary = next(m for m in sm.find_mutants(src, "F.swift") if m.operator == "boundary")
        assert (boundary.line, boundary.column, boundary.where) == (2, 5, "F.swift:2:5")
        assert boundary.apply(src) == "let a = 1\nif b <= c {}"

    def test_apply_refuses_a_stale_mutant(self):
        mutant = sm.find_mutants("a == b")[0]
        with pytest.raises(AssertionError):
            mutant.apply("a != b")


_TOKENS = ["a", "b", " == ", " < ", " && ", " + ", "true", "!x", "0", ".first", "min(", ")",
           "// c == d\n", '"s < t"', "/* x */", "\n", " ", "try!", '#"r == s"#']


@settings(max_examples=500)
@given(st.lists(st.sampled_from(_TOKENS), max_size=25))
def test_each_mutant_changes_one_code_spot(tokens):
    """Property: applying a mutant changes the text only at its own offset, never
    inside a comment or string, and the change is exactly original -> replacement."""
    src = "".join(tokens)
    mask = sm.code_mask(src)
    for m in sm.find_mutants(src):
        out = m.apply(src)
        assert out[:m.start] == src[:m.start]
        assert out[m.start + len(m.replacement):] == src[m.start + len(m.original):]
        assert all(mask[m.start:m.start + len(m.original)])
        assert m.original != m.replacement


@settings(max_examples=300)
@given(st.text(alphabet='ab"#/*\\\n =<!', max_size=40))
def test_mask_never_crashes_and_keeps_length(src):
    """Property: any text, however malformed, gets a mask of the same length."""
    assert len(sm.code_mask(src)) == len(src)
    sm.find_mutants(src)


class TestChangedLines:
    def test_hunks(self):
        diff = (
            "diff --git a/x/A.swift b/x/A.swift\n--- a/x/A.swift\n+++ b/x/A.swift\n"
            "@@ -3,0 +4,2 @@\n+a\n+b\n@@ -10 +12 @@\n-c\n+d\n"
            "--- a/x/Gone.swift\n+++ /dev/null\n@@ -1,3 +0,0 @@\n"
            "--- /dev/null\n+++ b/x/New.swift\n@@ -0,0 +1,3 @@\n"
        )
        assert sm.changed_lines(diff) == {"x/A.swift": {4, 5, 12}, "x/New.swift": {1, 2, 3}}

    def test_pure_deletion_adds_no_lines(self):
        assert sm.changed_lines("+++ b/A.swift\n@@ -5,2 +4,0 @@\n") == {"A.swift": set()}


def _package(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    (pkg / "Sources" / "Core").mkdir(parents=True)
    (pkg / "Sources" / "Core" / "A.swift").write_text("let a = 1\n")
    (pkg / "Sources" / "Core" / "B.swift").write_text("let b = true\n")
    subprocess.run(["git", "init", "-q"], cwd=pkg, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x", "--allow-empty"],
                   cwd=pkg, check=True)
    subprocess.run(["git", "add", "-A"], cwd=pkg, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "y"], cwd=pkg, check=True)
    return pkg


class TestCollect:
    def test_all_filtered_and_since(self, tmp_path):
        pkg = _package(tmp_path)
        assert [m.file for m in sm.collect(pkg, "Sources/Core", None, None)] == [
            "Sources/Core/A.swift", "Sources/Core/B.swift"]
        assert [m.file for m in sm.collect(pkg, "Sources/Core", None, "b.swift")] == ["Sources/Core/B.swift"]
        assert sm.collect(pkg, "Sources/Core", "HEAD", None) == []
        (pkg / "Sources" / "Core" / "A.swift").write_text("let a = 1\nlet c = 0\n")
        assert [(m.file, m.line) for m in sm.collect(pkg, "Sources/Core", "HEAD", None)] == [
            ("Sources/Core/A.swift", 2)]

    def test_list_mode(self, tmp_path, capsys):
        pkg = _package(tmp_path)
        assert sm.main(["--package", str(pkg), "--sources", "Sources/Core", "--list"]) == 0
        assert "2 mutants" in capsys.readouterr().out

    def test_nothing_to_run(self, tmp_path):
        pkg = _package(tmp_path)
        assert sm.main(["--package", str(pkg), "--sources", "Sources/Core", "--filter", "zzz"]) == 1


class TestRunMutant:
    """run_mutant with the swift calls stubbed: outcomes and that the file is restored."""

    def _setup(self, tmp_path, monkeypatch, build_rc, test_result):
        pkg = tmp_path
        (pkg / "A.swift").write_text("x == y")
        monkeypatch.setattr(sm, "build", lambda p: subprocess.CompletedProcess([], build_rc))
        monkeypatch.setattr(sm, "test", lambda p, timeout: test_result)
        return pkg, sm.find_mutants("x == y", "A.swift")[0]

    @pytest.mark.parametrize("build_rc, test_result, outcome", [
        (1, None, "UNVIABLE"),
        (0, None, "KILLED"),  # timed out
        (0, subprocess.CompletedProcess([], 1), "KILLED"),
        (0, subprocess.CompletedProcess([], 0), "SURVIVED"),
    ])
    def test_outcomes_restore_the_file(self, tmp_path, monkeypatch, build_rc, test_result, outcome):
        pkg, mutant = self._setup(tmp_path, monkeypatch, build_rc, test_result)
        assert sm.run_mutant(pkg, mutant, 1) == outcome
        assert (pkg / "A.swift").read_text() == "x == y"

    def test_restores_even_when_interrupted(self, tmp_path, monkeypatch):
        pkg, mutant = self._setup(tmp_path, monkeypatch, 0, None)

        def boom(p, timeout):
            assert (pkg / "A.swift").read_text() == "x != y"
            raise KeyboardInterrupt

        monkeypatch.setattr(sm, "test", boom)
        with pytest.raises(KeyboardInterrupt):
            sm.run_mutant(pkg, mutant, 1)
        assert (pkg / "A.swift").read_text() == "x == y"


class TestMain:
    def _stub(self, monkeypatch, build_rcs, test_rcs):
        builds, tests = iter(build_rcs), iter(test_rcs)
        monkeypatch.setattr(sm, "build", lambda p: subprocess.CompletedProcess([], next(builds)))

        def fake_test(p, timeout):
            rc = next(tests)
            return None if rc is None else subprocess.CompletedProcess([], rc)

        monkeypatch.setattr(sm, "test", fake_test)

    def test_report_and_exit_code(self, tmp_path, monkeypatch, capsys):
        pkg = _package(tmp_path)
        # baseline build, baseline test; then per mutant: build + test. A: 1 -> 0 survives; B: true -> false killed.
        self._stub(monkeypatch, [0, 0, 0], [0, 0, 1])
        report = tmp_path / "r.json"
        rc = sm.main(["--package", str(pkg), "--sources", "Sources/Core", "--report", str(report)])
        out = capsys.readouterr().out
        assert rc == 1 and "1/2 viable mutants killed (50.0%)" in out and "survivor: Sources/Core/A.swift:1" in out
        assert [r["outcome"] for r in __import__("json").loads(report.read_text())] == ["SURVIVED", "KILLED"]

    def test_all_killed_exits_zero(self, tmp_path, monkeypatch, capsys):
        pkg = _package(tmp_path)
        self._stub(monkeypatch, [0, 0, 1], [0, 1])
        assert sm.main(["--package", str(pkg), "--sources", "Sources/Core"]) == 0
        assert "1/1 viable mutants killed (100.0%), 1 didn't compile" in capsys.readouterr().out

    def test_baseline_failures_stop_the_run(self, tmp_path, monkeypatch):
        pkg = _package(tmp_path)
        self._stub(monkeypatch, [1], [])
        assert sm.main(["--package", str(pkg), "--sources", "Sources/Core"]) == 1
        self._stub(monkeypatch, [0], [1])
        assert sm.main(["--package", str(pkg), "--sources", "Sources/Core"]) == 1
        self._stub(monkeypatch, [0], [None])
        assert sm.main(["--package", str(pkg), "--sources", "Sources/Core"]) == 1


def test_build_and_test_wrappers(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, cwd, capture_output, text, timeout=None):
        calls.append((cmd, timeout))
        if timeout == 0.01:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(sm.subprocess, "run", fake_run)
    assert sm.build(tmp_path).returncode == 0
    assert sm.test(tmp_path, 5).returncode == 0
    assert sm.test(tmp_path, 0.01) is None
    assert [c[0][:2] for c in calls] == [["swift", "build"], ["swift", "test"], ["swift", "test"]]


def test_rerun_keeps_only_survivors_that_still_apply(tmp_path):
    src = "a == b && c"
    mutants = sm.find_mutants(src, "A.swift")
    report = tmp_path / "r.json"
    rows = [{**sm.asdict(m), "outcome": o} for m, o in zip(mutants, ["SURVIVED", "KILLED"])]
    rows.append({**rows[0], "start": 99})  # moved since: no longer matches
    report.write_text(__import__("json").dumps(rows))
    assert sm.survivors_of(report, mutants) == [mutants[0]]


def test_main_rerun_with_nothing_left(tmp_path):
    pkg = _package(tmp_path)
    report = tmp_path / "r.json"
    report.write_text("[]")
    assert sm.main(["--package", str(pkg), "--sources", "Sources/Core", "--rerun", str(report)]) == 1
