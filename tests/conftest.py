"""Fixtures that fake Claude Code's on-disk layout (task outputs + transcripts)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from claude_report.discovery import project_slug


@dataclass
class FakeSession:
    """One conversation: writes transcript events and task output files."""

    root: FakeClaude
    cwd: str
    id: str
    _tool_counter: int = field(default=0)

    @property
    def slug(self) -> str:
        return project_slug(self.cwd)

    @property
    def transcript(self) -> Path:
        return self.root.projects / self.slug / f"{self.id}.jsonl"

    @property
    def tasks_dir(self) -> Path:
        return self.root.tmp_root / self.slug / self.id / "tasks"

    def event(self, event: dict[str, object]) -> None:
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def raw(self, line: str) -> None:
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def title(self, title: str, *, custom: bool = False) -> None:
        key = "customTitle" if custom else "aiTitle"
        self.event({"type": "custom-title" if custom else "ai-title", key: title})

    def add_task(
        self,
        task_id: str,
        command: str = "",
        description: str = "",
        output: str = "",
        *,
        status: str | None = None,
        mtime: float | None = None,
        in_transcript: bool = True,
    ) -> Path:
        """Create a task output file and, by default, the transcript events that launched it."""
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        out = self.tasks_dir / f"{task_id}.output"
        out.write_text(output)
        if mtime is not None:
            os.utime(out, (mtime, mtime))
        if in_transcript:
            self._tool_counter += 1
            tool_id = f"toolu_{self.id}_{self._tool_counter}"
            self.event(
                {
                    "type": "assistant",
                    "cwd": self.cwd,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": "Bash",
                                "input": {
                                    "command": command,
                                    "description": description,
                                    "run_in_background": True,
                                },
                            }
                        ],
                    },
                }
            )
            self.event(
                {
                    "type": "user",
                    "cwd": self.cwd,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": f"Command running in background with ID: {task_id}.",
                            }
                        ],
                    },
                    "toolUseResult": {"backgroundTaskId": task_id, "stdout": ""},
                }
            )
        if status is not None:
            self.finish(task_id, status)
        return out

    def add_foreground(
        self, task_id: str, command: str, description: str = "", output: str = ""
    ) -> str:
        """A running foreground command: an output file and a Bash call with no result yet.

        Returns the tool-call id, to pass to :meth:`end_foreground`.
        """
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        (self.tasks_dir / f"{task_id}.output").write_text(output)
        self._tool_counter += 1
        tool_id = f"toolu_{self.id}_{self._tool_counter}"
        self.event(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "Bash",
                            "input": {"command": command, "description": description},
                        }
                    ],
                },
            }
        )
        return tool_id

    def end_foreground(self, tool_id: str, *, is_error: bool = False) -> None:
        """The tool result Claude Code records when a foreground command returns."""
        self.event(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": "Exit code 2" if is_error else "ok",
                            "is_error": is_error,
                        }
                    ],
                },
            }
        )

    def finish(self, task_id: str, status: str = "completed") -> None:
        """Write the completion notification Claude Code records when a task ends."""
        self.event(
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                f"<status>{status}</status>\n</task-notification>",
            }
        )


@dataclass
class FakeClaude:
    """Root of a fake Claude Code installation (config dir + temp dir)."""

    tmp_root: Path
    projects: Path

    def session(self, cwd: str, sid: str, title: str = "") -> FakeSession:
        session = FakeSession(self, cwd, sid)
        session.event({"type": "user", "cwd": cwd, "message": {"role": "user", "content": "hi"}})
        if title:
            session.title(title)
        return session


@pytest.fixture
def claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    """A fake Claude Code layout; environment variables point claude-report at it."""
    config = tmp_path / "config"
    tmp_root = tmp_path / "tmp"
    (config / "projects").mkdir(parents=True)
    tmp_root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLAUDE_REPORT_TMP_ROOT", str(tmp_root))
    monkeypatch.delenv("NO_COLOR", raising=False)
    return FakeClaude(tmp_root=tmp_root, projects=config / "projects")
