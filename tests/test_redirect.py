"""Detection of log files that a command redirects its output to."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_report.discovery import redirect_target


@pytest.fixture
def log(tmp_path: Path) -> Path:
    path = tmp_path / "run.log"
    path.write_text("output\n")
    return path


@pytest.mark.parametrize(
    "template",
    [
        "pytest > {log}",
        "pytest >{log}",
        "pytest >> {log}",
        "pytest 2> {log}",
        "pytest > {log} 2>&1",
        "pytest 2>&1 > {log}",
        "pytest &> {log}",
        "pytest &>> {log}",
        "pytest 2>&1 | tee {log}",
        "pytest | tee -a {log}",
        'pytest > "{log}"',
        "pytest > '{log}'; echo EXIT=$?",
        "(cd x && pytest > {log})",
        "pytest > /dev/null; make > {log}",
    ],
)
def test_detects_redirect(template: str, log: Path) -> None:
    assert redirect_target(template.format(log=log)) == log


@pytest.mark.parametrize(
    "command",
    [
        "",
        "pytest -q",
        "pytest 2>&1",
        "pytest > /dev/null",
        "pytest > /does/not/exist.log",
    ],
)
def test_no_usable_redirect(command: str) -> None:
    assert redirect_target(command) is None


def test_directory_is_not_a_log_file(tmp_path: Path) -> None:
    assert redirect_target(f"ls > {tmp_path}") is None


def test_relative_path_resolved_against_cwd(log: Path) -> None:
    assert redirect_target("pytest > run.log", cwd=log.parent) == log


def test_relative_path_without_cwd_uses_process_cwd(
    log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(log.parent)
    assert redirect_target("pytest > run.log") == Path("run.log")


def test_home_is_expanded(log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(log.parent))
    assert redirect_target("pytest > ~/run.log") == log


def test_first_existing_target_wins(tmp_path: Path, log: Path) -> None:
    other = tmp_path / "other.log"
    other.write_text("")
    assert redirect_target(f"a > /missing.log; b > {other}; c > {log}") == other


# --------------------------------------------------------------------------- variables


@pytest.mark.parametrize(
    "template",
    [
        "S={dir}\nmake > $S/run.log 2>&1",
        "S={dir}; make > ${{S}}/run.log",
        "export S={dir} X=1 && make > $S/run.log",
        'S="{dir}" && make | tee "$S/run.log"',
        "S='{dir}' make &> $S/run.log",
        "D={dir}; F=$D/run.log; make > $F",
    ],
)
def test_variables_assigned_in_command_are_expanded(template: str, log: Path) -> None:
    assert redirect_target(template.format(dir=log.parent)) == log


def test_environment_variables_are_expanded(log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOGDIR", str(log.parent))
    assert redirect_target("make > $LOGDIR/run.log") == log


def test_command_assignment_beats_environment(log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S", "/nowhere")
    assert redirect_target(f"S={log.parent}; make > $S/run.log") == log


def test_unknown_variable_is_not_a_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNSET_VAR", raising=False)
    assert redirect_target("make > $UNSET_VAR/run.log") is None


def test_single_quoted_value_is_literal(tmp_path: Path) -> None:
    literal = tmp_path / "$HOME"
    literal.mkdir()
    (literal / "run.log").write_text("")
    assert redirect_target(f"S='{literal}'; make > $S/run.log") == literal / "run.log"
