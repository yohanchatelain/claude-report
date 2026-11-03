"""Discovery of conversations and tasks on disk."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from claude_report.discovery import (
    all_tasks,
    config_dir,
    discover,
    project_slug,
    tmp_root,
)
from tests.conftest import FakeClaude


def test_default_locations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_REPORT_TMP_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "t"))
    (tmp_path / "t").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", None)  # recompute from TMPDIR
    assert config_dir() == tmp_path / ".claude"
    assert tmp_root() == tmp_path / "t" / f"claude-{os.getuid()}"


@pytest.mark.parametrize(
    ("path", "slug"),
    [
        ("/home/u/Work", "-home-u-Work"),
        ("/home/u/my.project_v2", "-home-u-my-project-v2"),
        (Path("/a b/c"), "-a-b-c"),
    ],
)
def test_project_slug(path: str | Path, slug: str) -> None:
    assert project_slug(path) == slug


def test_nothing_on_disk(claude: FakeClaude) -> None:
    assert discover() == []


def test_missing_tmp_root(claude: FakeClaude, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_REPORT_TMP_ROOT", str(claude.tmp_root / "absent"))
    assert discover() == []


def test_sessions_across_projects_sorted_by_activity(claude: FakeClaude) -> None:
    a = claude.session("/work/a", "sa", title="Alpha")
    b = claude.session("/work/b", "sb", title="Beta")
    a.add_task("a1", mtime=2_000)
    b.add_task("b1", mtime=1_000)

    sessions = discover()

    assert [s.title for s in sessions] == ["Beta", "Alpha"]
    assert [s.cwd for s in sessions] == ["/work/b", "/work/a"]


def test_tasks_carry_metadata_and_size(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.add_task("b1", "make", "Build", output="12345", status="completed")

    (task,) = discover()[0].tasks

    assert (task.id, task.command, task.description) == ("b1", "make", "Build")
    assert (task.size, task.status, task.finished) == (5, "completed", True)
    assert task.output == s.tasks_dir / "b1.output"


def test_task_unknown_to_transcript_is_running(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.add_task("b1", in_transcript=False)
    (task,) = discover()[0].tasks
    assert (task.status, task.label) == ("running", "(unknown command)")


def test_session_without_transcript_still_listed(claude: FakeClaude) -> None:
    out = claude.tmp_root / "-w" / "lost-session" / "tasks" / "b1.output"
    out.parent.mkdir(parents=True)
    out.write_text("x")
    (session,) = discover()
    assert (session.label, session.cwd) == ("session lost-ses", "")


def test_here_keeps_current_project_and_parents(
    claude: FakeClaude, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    (project / "sub").mkdir(parents=True)
    claude.session(str(project), "mine").add_task("m1")
    claude.session("/elsewhere", "other").add_task("o1")

    assert [s.id for s in discover(here=project / "sub")] == ["mine"]
    assert [s.id for s in discover(here=project)] == ["mine"]


@pytest.mark.parametrize("query", ["abc", "FAKE APP", "fakeapp"])
def test_session_query(claude: FakeClaude, query: str) -> None:
    claude.session("/work/fakeapp", "abc123", title="Rebuild Fake App").add_task("t1")
    claude.session("/work/other", "zzz999", title="Other").add_task("t2")
    assert [s.id for s in discover(session_query=query)] == ["abc123"]


def test_session_query_id_must_be_prefix(claude: FakeClaude) -> None:
    claude.session("/w", "abc123").add_task("t1")
    assert discover(session_query="123") == []


def test_refresh_skips_outputs_deleted_since_listing(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    keep = s.add_task("keep")
    gone = s.add_task("gone")
    (session,) = discover()
    gone.unlink()
    assert [t.output for t in session.refresh().tasks] == [keep]


def test_session_properties(claude: FakeClaude) -> None:
    s = claude.session("/w", "0123456789abcdef")
    s.add_task("t1", status="completed", mtime=10)
    s.add_task("t2", mtime=30)
    (session,) = discover()
    assert session.label == "session 01234567"
    assert session.mtime == 30
    assert [t.id for t in session.running] == ["t2"]


def test_all_tasks_orders_by_mtime_across_sessions(claude: FakeClaude) -> None:
    claude.session("/a", "sa").add_task("late", mtime=300)
    sb = claude.session("/b", "sb")
    sb.add_task("early", mtime=100)
    sb.add_task("middle", mtime=200)
    assert [t.id for t in all_tasks(discover())] == ["early", "middle", "late"]


def test_log_file_follows_redirect_relative_to_session_cwd(
    claude: FakeClaude, tmp_path: Path
) -> None:
    (tmp_path / "logs").mkdir()
    log = tmp_path / "logs" / "pytest.log"
    log.write_text("collected 3 items\n")
    claude.session(str(tmp_path), "s1").add_task("t1", "pytest > logs/pytest.log 2>&1")

    (task,) = discover()[0].tasks

    assert task.log_file == log


def test_current_status_rereads_transcript(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.add_task("t1")
    (task,) = discover()[0].tasks
    assert task.current_status() == "running"
    s.finish("t1", "failed")
    assert task.current_status() == "failed"


# --------------------------------------------------------------------------- foreground


def test_running_foreground_command_is_identified(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.add_task("bg", "pytest", "Tests", mtime=100)
    tool_id = s.add_foreground("fg", "make -j1", "Build fakeapp")

    tasks = {t.id: t for t in discover()[0].tasks}

    assert (tasks["fg"].command, tasks["fg"].description) == ("make -j1", "Build fakeapp")
    assert tasks["fg"].tool_use_id == tool_id
    assert tasks["fg"].foreground
    assert not tasks["bg"].foreground
    assert tasks["bg"].command == "pytest"


def test_foreground_pairs_newest_with_newest(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.add_foreground("old", "first")
    s.add_foreground("new", "second")
    os.utime(s.tasks_dir / "old.output", (100, 100))
    os.utime(s.tasks_dir / "new.output", (200, 200))
    tasks = {t.id: t.command for t in discover()[0].tasks}
    assert tasks == {"old": "first", "new": "second"}


def test_finished_foreground_leftover_is_unknown(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.end_foreground(s.add_foreground("fg", "make"))
    (task,) = discover()[0].tasks
    assert (task.foreground, task.label) == (False, "(unknown command)")


def test_foreground_status_comes_from_tool_result(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    tool_id = s.add_foreground("fg", "make")
    (task,) = discover()[0].tasks
    assert task.current_status() == "running"
    s.end_foreground(tool_id, is_error=True)
    assert task.current_status() == "failed"
