"""Parsing of Claude Code transcripts."""

from __future__ import annotations

from pathlib import Path

from claude_report.discovery import RUNNING, parse_transcript
from tests.conftest import FakeClaude


def test_missing_transcript_gives_empty_info(tmp_path: Path) -> None:
    info = parse_transcript(tmp_path / "nope.jsonl")
    assert (info.title, info.cwd, info.tasks) == ("", "", {})


def test_task_description_command_and_status(claude: FakeClaude) -> None:
    s = claude.session("/work/proj", "s1", title="Fix the build")
    s.add_task("b1", "make -j8", "Build everything", status="failed")
    s.add_task("b2", "pytest -q", "Run tests")

    info = parse_transcript(s.transcript)

    assert info.title == "Fix the build"
    assert info.cwd == "/work/proj"
    assert info.tasks["b1"].description == "Build everything"
    assert info.tasks["b1"].command == "make -j8"
    assert info.tasks["b1"].status == "failed"
    assert info.tasks["b2"].status == RUNNING


def test_latest_ai_title_wins(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1", title="First guess")
    s.title("Better title")
    assert parse_transcript(s.transcript).title == "Better title"


def test_custom_title_beats_ai_title_regardless_of_order(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.title("My name", custom=True)
    s.title("Generated later")
    assert parse_transcript(s.transcript).title == "My name"


def test_escaped_characters_are_decoded(claude: FakeClaude) -> None:
    s = claude.session('/w/with "quotes"', "s1", title='Use "uv" — café\\path')
    info = parse_transcript(s.transcript)
    assert info.title == 'Use "uv" — café\\path'
    assert info.cwd == '/w/with "quotes"'


def test_first_cwd_is_kept(claude: FakeClaude) -> None:
    s = claude.session("/first", "s1")
    s.event({"type": "user", "cwd": "/second"})
    assert parse_transcript(s.transcript).cwd == "/first"


def test_malformed_and_unexpected_lines_are_ignored(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.raw('{"type": "tool_use", truncated')
    s.raw('["tool_use", "backgroundTaskId"]')
    s.raw('{"message": {"content": "tool_use as text"}}')
    s.raw('{"message": {"content": ["tool_use", 3]}, "toolUseResult": "backgroundTaskId"}')
    s.add_task("b1", "echo ok", "Echo")
    assert parse_transcript(s.transcript).tasks["b1"].command == "echo ok"


def test_notification_without_launch_event_still_gives_status(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.finish("orphan", "killed")
    task = parse_transcript(s.transcript).tasks["orphan"]
    assert (task.status, task.description) == ("killed", "")


def test_later_notification_overrides_earlier(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.add_task("b1", "x", status="failed")
    s.finish("b1", "completed")
    assert parse_transcript(s.transcript).tasks["b1"].status == "completed"


def test_pending_foreground_calls_and_results(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    done = s.add_foreground("f1", "make lint", "Lint")
    failed = s.add_foreground("f2", "make test", "Test")
    running = s.add_foreground("f3", "make build", "Build")
    s.end_foreground(done)
    s.end_foreground(failed, is_error=True)

    info = parse_transcript(s.transcript)

    assert [(p.command, p.tool_use_id) for p in info.pending] == [("make build", running)]
    assert info.status_of("f1", done) == "completed"
    assert info.status_of("f2", failed) == "failed"
    assert info.status_of("f3", running) == RUNNING


def test_background_calls_are_not_pending(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.add_task("b1", "pytest", "Tests")
    info = parse_transcript(s.transcript)
    assert info.pending == []
    assert info.status_of("b1") == RUNNING


def test_non_bash_tool_calls_are_not_pending(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.event(
        {
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t9", "name": "Agent", "input": {"prompt": "x"}}
                ]
            }
        }
    )
    assert parse_transcript(s.transcript).pending == []


def test_notification_status_beats_tool_result(claude: FakeClaude) -> None:
    s = claude.session("/w", "s1")
    s.add_task("b1", status="failed")
    assert parse_transcript(s.transcript).status_of("b1", "anything") == "failed"
