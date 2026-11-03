"""Command-line behaviour: formatting, task selection, following and the entry point."""

from __future__ import annotations

import io
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_report import __version__, cli
from claude_report.cli import (
    EXIT_INTERRUPTED,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_TASK_FAILED,
    NotFoundError,
    Style,
    follow,
    format_age,
    format_size,
    main,
    select_task,
    tail_bytes,
    task_matches,
)
from claude_report.discovery import Task, discover
from tests.conftest import FakeClaude

PLAIN = Style(False)


def run(argv: list[str], picker: cli.Picker = lambda tasks: None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdout=out, stderr=err, picker=picker)
    return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- formatting


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (-5, "0s"),
        (0, "0s"),
        (59, "59s"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h00"),
        (3 * 3600 + 5 * 60, "3h05"),
        (86400, "1d"),
        (10 * 86400, "10d"),
    ],
)
def test_format_age(seconds: float, text: str) -> None:
    assert format_age(seconds) == text


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0B"),
        (1023, "1023B"),
        (1024, "1KB"),
        (41 * 1024, "41KB"),
        (5 * 1024**2, "5MB"),
        (int(1.5 * 1024**3), "1.5GB"),
    ],
)
def test_format_size(size: int, text: str) -> None:
    assert format_size(size) == text


def test_style_enabled_and_disabled() -> None:
    assert Style(True)("x", "1") == "\033[1mx\033[0m"
    assert PLAIN("x", "1") == "x"
    assert PLAIN.status("failed", 8) == "failed  "


def test_style_for_stream_respects_tty_and_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    assert Style.for_stream(Tty()).enabled
    assert not Style.for_stream(io.StringIO()).enabled
    monkeypatch.setenv("NO_COLOR", "1")
    assert not Style.for_stream(Tty()).enabled


@pytest.mark.parametrize(
    ("lines", "expected"),
    [(-1, b"a\nb\nc"), (0, b""), (2, b"b\nc"), (10, b"a\nb\nc")],
)
def test_tail_bytes(lines: int, expected: bytes) -> None:
    assert tail_bytes(b"a\nb\nc", lines) == expected


# --------------------------------------------------------------------------- selection


@pytest.fixture
def tasks(claude: FakeClaude) -> FakeClaude:
    """Two conversations: an old finished build, a test run and a running build."""
    app = claude.session("/work/fakeapp", "sess-fakeapp", title="Rebuild Fake App")
    app.add_task("build1", "cmake --build .", "Build Fake App", status="failed", mtime=100)
    app.add_task("test1", "uv run pytest -q", "Run the test suite", status="completed", mtime=200)
    web = claude.session("/work/web", "sess-web", title="Website")
    web.add_task("build2", "npm run build", "Build the site", mtime=300)
    return claude


def ids(found: list[Task]) -> list[str]:
    return [t.id for t in found]


def test_task_matches_fields(tasks: FakeClaude) -> None:
    by_id = {t.id: t for s in discover() for t in s.tasks}
    assert task_matches(by_id["test1"], "test1")
    assert task_matches(by_id["test1"], "PYTEST")  # command, case-insensitive
    assert task_matches(by_id["test1"], "suite")  # description
    assert task_matches(by_id["test1"], "fake app")  # conversation title
    assert not task_matches(by_id["test1"], "npm")


def test_select_defaults_to_running_task(tasks: FakeClaude) -> None:
    assert select_task(discover()).id == "build2"


def test_select_newest_match_when_none_running(tasks: FakeClaude) -> None:
    assert select_task(discover(), "fake app").id == "test1"


def test_select_prefers_running_match_over_newer_finished(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.add_task("old", "make", mtime=100)
    s.add_task("new", "make", status="completed", mtime=200)
    assert select_task(discover(), "make").id == "old"


def test_select_falls_back_to_newest_when_nothing_running(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.add_task("a", status="completed", mtime=1)
    s.add_task("b", status="failed", mtime=2)
    assert select_task(discover()).id == "b"


def test_select_no_match(tasks: FakeClaude) -> None:
    with pytest.raises(NotFoundError, match="no task matching 'nothing'"):
        select_task(discover(), "nothing")


def test_select_no_tasks() -> None:
    with pytest.raises(NotFoundError, match="no background task"):
        select_task([])


def test_several_running_tasks_ask_the_picker(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.add_task("r1", mtime=1)
    s.add_task("r2", mtime=2)
    s.add_task("done", status="completed", mtime=3)
    offered: list[list[str]] = []

    def picker(candidates: list[Task]) -> Task:
        offered.append(ids(candidates))
        return candidates[0]

    assert select_task(discover(), picker=picker).id == "r1"
    assert offered == [["r1", "r2"]]


def test_without_picker_newest_running_task_wins(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.add_task("r1", mtime=1)
    s.add_task("r2", mtime=2)
    assert select_task(discover(), picker=lambda _: None).id == "r2"


def test_pick_offers_finished_tasks_too(tasks: FakeClaude) -> None:
    offered: list[list[str]] = []

    def picker(candidates: list[Task]) -> None:
        offered.append(ids(candidates))

    assert select_task(discover(), "build", pick=True, picker=picker).id == "build2"
    # test1 matches through its conversation title, "Rebuild Fake App".
    assert offered == [["build1", "test1", "build2"]]


def test_fzf_picker_unavailable_without_terminal(
    tasks: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    assert cli.fzf_picker(discover()[0].tasks) is None


def test_fzf_picker_parses_selection(tasks: FakeClaude, monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(sys, "stderr", Tty())
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/fzf")
    calls: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(str(kwargs["input"]))
        return subprocess.CompletedProcess(cmd, 0, stdout="\033[1mtest1\033[0m  completed ...\n")

    monkeypatch.setattr("subprocess.run", fake_run)
    candidates = [t for s in discover() for t in s.tasks]

    chosen = cli.fzf_picker(candidates)

    assert chosen is not None
    assert chosen.id == "test1"
    assert "Rebuild Fake App" in calls[0]  # entries show the conversation title

    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 130, stdout=""),
    )
    with pytest.raises(KeyboardInterrupt):
        cli.fzf_picker(candidates)


# --------------------------------------------------------------------------- follow


def follow_task(task: Task, lines: int) -> tuple[int, bytes, str]:
    out, err = io.BytesIO(), io.StringIO()
    code = follow(
        task, lines=lines, out=out, err=err, style=PLAIN, poll_interval=0.01, status_interval=0.05
    )
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize(
    ("status", "code"),
    [("completed", EXIT_OK), ("failed", EXIT_TASK_FAILED), ("killed", EXIT_TASK_FAILED)],
)
def test_follow_finished_task_returns_immediately(
    claude: FakeClaude, status: str, code: int
) -> None:
    s = claude.session("/w", "s1", title="Conv")
    s.add_task("t1", "make", "Build", output="1\n2\n3\n", status=status)
    (task,) = discover()[0].tasks

    result = follow_task(task, lines=2)

    assert result[0] == code
    assert result[1] == b"2\n3\n"
    assert "==> t1 [" + status + "] Build" in result[2]
    assert "conversation: Conv  (/w)" in result[2]
    assert "$ make" in result[2]
    assert result[2].rstrip().endswith(f"==> t1 {status}")


def test_follow_streams_until_task_ends(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    out_file = s.add_task("t1", "pytest", output="start\n")
    (task,) = discover()[0].tasks

    def agent() -> None:
        for i in range(3):
            time.sleep(0.03)
            with open(out_file, "a") as fh:
                fh.write(f"line {i}\n")
        with open(out_file, "a") as fh:
            fh.write("PASSED\n")
        s.finish("t1", "completed")

    thread = threading.Thread(target=agent)
    thread.start()
    code, out, err = follow_task(task, lines=-1)
    thread.join()

    assert code == EXIT_OK
    assert out == b"start\nline 0\nline 1\nline 2\nPASSED\n"
    assert err.rstrip().endswith("==> t1 completed")


def test_follow_reads_redirected_log(claude: FakeClaude, tmp_path: Path) -> None:
    log = tmp_path / "pytest.log"
    log.write_bytes(b"collected 2 items\n\xff binary ok\n")
    s = claude.session(str(tmp_path), "s1")
    s.add_task("t1", f"pytest > {log} 2>&1", status="completed")
    (task,) = discover()[0].tasks

    code, out, err = follow_task(task, lines=-1)

    assert code == EXIT_OK
    assert out == b"collected 2 items\n\xff binary ok\n"
    assert str(log) in err


# --------------------------------------------------------------------------- main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"claude-report {__version__}"


def test_actions_are_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["-f", "x", "-p", "y"])
    assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_list_without_tasks(claude: FakeClaude) -> None:
    code, _out, err = run([])
    assert code == EXIT_NOT_FOUND
    assert "no conversation with background tasks" in err


def test_list_shows_conversations_and_tasks(tasks: FakeClaude) -> None:
    code, out, _ = run([])
    assert code == EXIT_OK
    assert out.index("Rebuild Fake App") < out.index("Website")  # newest last
    assert "/work/fakeapp  ·  sess-fakeapp" in out
    assert "Website  (1 running)" in out
    for expected in ("build1  failed", "test1  completed", "build2  running", "Build the site"):
        assert expected in out
    assert "\033[" not in out  # no colour when not a terminal


def test_list_limit(tasks: FakeClaude) -> None:
    _, out, _ = run(["-l", "1"])
    assert "Website" in out
    assert "Rebuild Fake App" not in out
    assert "(1 older conversations hidden" in out
    _, out, _ = run(["-l", "0"])
    assert "Rebuild Fake App" in out
    assert "hidden" not in out


def test_session_filter(tasks: FakeClaude) -> None:
    code, out, _ = run(["-s", "fakeapp"])
    assert code == EXIT_OK
    assert "Rebuild Fake App" in out
    assert "Website" not in out
    code, _, _err = run(["-s", "nothing"])
    assert code == EXIT_NOT_FOUND


def test_here_filter(tasks: FakeClaude, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    code, _, _err = run(["--here"])
    assert code == EXIT_NOT_FOUND


def test_path_prints_log_file(tasks: FakeClaude) -> None:
    code, out, _ = run(["--path", "test1"])
    assert code == EXIT_OK
    assert out.strip().endswith("sess-fakeapp/tasks/test1.output")


def test_print_writes_whole_output(claude: FakeClaude) -> None:
    claude.session("/w", "s1").add_task("t1", "make", output="a\nb\n", status="completed")
    code, out, err = run(["-p", "t1"])
    assert (code, out) == (EXIT_OK, "a\nb\n")
    assert "==> t1" in err


def test_follow_unknown_task(tasks: FakeClaude) -> None:
    code, _, err = run(["-f", "nothing"])
    assert code == EXIT_NOT_FOUND
    assert "no task matching 'nothing'" in err


def test_follow_failed_task_exit_code(tasks: FakeClaude) -> None:
    code, _, err = run(["-f", "build1"])
    assert code == EXIT_TASK_FAILED
    assert "build1 failed" in err


def test_pick_uses_picker(tasks: FakeClaude) -> None:
    chosen: list[str] = []

    def picker(candidates: list[Task]) -> Task:
        chosen.append(candidates[0].id)
        return candidates[0]

    code, _, _err = run(["--pick"], picker=picker)
    assert code == EXIT_TASK_FAILED  # build1 is the oldest candidate and failed
    assert chosen == ["build1"]


def test_keyboard_interrupt(tasks: FakeClaude) -> None:
    def picker(candidates: list[Task]) -> Task:
        raise KeyboardInterrupt

    code, _, _ = run(["--pick"], picker=picker)
    assert code == EXIT_INTERRUPTED


def test_broken_pipe_is_silent(tasks: FakeClaude) -> None:
    class Closed(io.StringIO):
        def write(self, s: str) -> int:
            raise BrokenPipeError

    assert main([], stdout=Closed(), stderr=io.StringIO()) == EXIT_OK


def test_binary_output_goes_to_buffer(claude: FakeClaude) -> None:
    claude.session("/w", "s1").add_task("t1", output="", status="completed")
    (claude.tmp_root / "-w/s1/tasks/t1.output").write_bytes(b"\xffraw\n")
    buffer = io.BytesIO()
    text = io.TextIOWrapper(buffer)
    assert main(["-p", "t1"], stdout=text, stderr=io.StringIO()) == EXIT_OK
    text.flush()
    assert buffer.getvalue() == b"\xffraw\n"


def test_module_entry_point(tasks: FakeClaude) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "claude_report", "--path", "test1"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert result.stdout.strip().endswith("test1.output")


def test_follow_foreground_until_tool_result(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    tool_id = s.add_foreground("fg", "make", "Build", output="compiling\n")
    (task,) = discover()[0].tasks

    def agent() -> None:
        time.sleep(0.05)
        with open(s.tasks_dir / "fg.output", "a") as fh:
            fh.write("linking\n")
        s.end_foreground(tool_id)

    thread = threading.Thread(target=agent)
    thread.start()
    code, out, err = follow_task(task, lines=-1)
    thread.join()

    assert code == EXIT_OK
    assert out == b"compiling\nlinking\n"
    assert err.rstrip().endswith("==> fg completed")


def test_list_marks_foreground_commands(claude: FakeClaude) -> None:
    claude.session("/w", "s1").add_foreground("fg", "make", "Build fakeapp")
    _, out, _ = run([])
    assert "Build fakeapp  (foreground)" in out


def test_multiline_command_layout(claude: FakeClaude) -> None:
    claude.session("/w", "s1").add_task("t1", "cd x &&\n  make -j8\nmake check", status="completed")
    _, out, err = run(["-p", "t1"])
    assert "$ cd x &&\n        make -j8\n      make check" in err
    _, out, _ = run([])
    assert "cd x && make -j8 make check" in out


def test_long_command_is_truncated_in_header(claude: FakeClaude) -> None:
    claude.session("/w", "s1").add_task("t1", "x" * 600, status="completed")
    _, _, err = run(["-p", "t1"])
    assert "x" * 500 + " ..." in err
    assert "x" * 501 not in err
