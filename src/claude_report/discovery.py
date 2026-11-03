"""Locate Claude Code conversations and their background tasks on disk.

Claude Code stores two things this module relies on:

* Each background command streams its output to
  ``<tmp root>/<project slug>/<session id>/tasks/<task id>.output``.
* Each conversation's transcript is a JSON-lines file at
  ``<config dir>/projects/<project slug>/<session id>.jsonl``. It records the tool call
  that started each task (description, command), the task-completion notification
  (status), the conversation title and its working directory.

Neither layout is a public API, so parsing is deliberately lenient: unknown or
malformed lines are skipped rather than treated as errors.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

_NOTIFICATION_RE = re.compile(r"<task-id>(\w+)</task-id>.*?<status>(\w+)</status>")
_TITLE_RE = re.compile(r'"(customTitle|aiTitle)"\s*:\s*"((?:[^"\\]|\\.)*)"')
_CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
# `> f`, `>> f`, `2> f`, `&> f`, `| tee f`, `| tee -a f`; the path stops at shell syntax.
_ASSIGNMENT_RE = re.compile(r"""(?:^|[\s;&|(])([A-Za-z_]\w*)=("[^"]*"|'[^']*'|[^\s;&|()]*)""")
_VARIABLE_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")
_REDIRECT_RE = re.compile(r"""(?:\d?>>?|&>>?|\btee(?:\s+-a)?)\s*["']?([^\s"';&|<>()]+)""")


# --------------------------------------------------------------------------- locations


def config_dir() -> Path:
    """Claude Code's configuration directory (honours ``CLAUDE_CONFIG_DIR``)."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def projects_dir() -> Path:
    """Directory holding one sub-directory of transcripts per project."""
    return config_dir() / "projects"


def tmp_root() -> Path:
    """Directory holding task outputs (override with ``CLAUDE_REPORT_TMP_ROOT``)."""
    override = os.environ.get("CLAUDE_REPORT_TMP_ROOT")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / f"claude-{os.getuid()}"


def project_slug(path: Path | str) -> str:
    """Name Claude Code gives a project directory: every non-alphanumeric char -> ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


# --------------------------------------------------------------------------- transcripts


@dataclass
class TaskInfo:
    """What the transcript says about one task.

    ``tool_use_id`` is set for foreground commands, whose task id the transcript does not
    record while they run; their status comes from the tool result instead.
    """

    description: str = ""
    command: str = ""
    status: str = RUNNING
    tool_use_id: str = ""


@dataclass
class TranscriptInfo:
    """Facts extracted from one conversation transcript."""

    title: str = ""
    cwd: str = ""
    tasks: dict[str, TaskInfo] = field(default_factory=dict)
    #: Bash calls still waiting for their result (foreground commands), oldest first.
    pending: list[TaskInfo] = field(default_factory=list)
    #: Tool-call id -> whether its result was an error (non-zero exit for Bash).
    results: dict[str, bool] = field(default_factory=dict)

    def status_of(self, task_id: str, tool_use_id: str = "") -> str:
        """Status of a task, falling back to its tool result for foreground commands."""
        if task_id in self.tasks:
            return self.tasks[task_id].status
        if tool_use_id in self.results:
            return FAILED if self.results[tool_use_id] else COMPLETED
        return RUNNING


def _json_unescape(raw: str) -> str:
    try:
        value = json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw
    return value if isinstance(value, str) else raw


def parse_transcript(path: Path) -> TranscriptInfo:
    """Extract title, working directory and task metadata from a transcript.

    A missing or unreadable transcript yields an empty :class:`TranscriptInfo`.
    A user-set title (``customTitle``) takes precedence over the generated one.
    """
    info = TranscriptInfo()
    ai_title = custom_title = ""
    calls = _ToolCalls()
    try:
        handle = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
    except OSError:
        return info
    with handle:
        for line in handle:
            for task_id, status in _NOTIFICATION_RE.findall(line):
                info.tasks.setdefault(task_id, TaskInfo()).status = status
            for kind, raw in _TITLE_RE.findall(line):
                if kind == "customTitle":
                    custom_title = _json_unescape(raw)
                else:
                    ai_title = _json_unescape(raw)
            if not info.cwd and (match := _CWD_RE.search(line)):
                info.cwd = _json_unescape(match.group(1))
            if "tool_use" in line or "tool_result" in line:
                calls.add_event(line)

    info.title = custom_title or ai_title
    info.results = calls.results
    for task_id, tool_use_id in calls.background.items():
        task = info.tasks.setdefault(task_id, TaskInfo())
        task.description, task.command = calls.describe(tool_use_id)
    for tool_use_id in calls.bash_order:
        if tool_use_id not in calls.results:
            description, command = calls.describe(tool_use_id)
            info.pending.append(TaskInfo(description, command, RUNNING, tool_use_id))
    return info


class _ToolCalls:
    """Tool calls and results seen in a transcript."""

    def __init__(self) -> None:
        self.inputs: dict[str, dict[str, object]] = {}
        self.bash_order: list[str] = []
        self.results: dict[str, bool] = {}
        self.background: dict[str, str] = {}  # task id -> tool-call id

    def describe(self, tool_use_id: str) -> tuple[str, str]:
        inputs = self.inputs.get(tool_use_id, {})
        return str(inputs.get("description") or ""), str(inputs.get("command") or "")

    def add_event(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        result = event.get("toolUseResult")
        task_id = result.get("backgroundTaskId") if isinstance(result, dict) else None
        for block in content:
            if not isinstance(block, dict):
                continue
            block_id = str(block.get("id"))
            if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                self.inputs[block_id] = block["input"]
                if block.get("name") == "Bash":
                    self.bash_order.append(block_id)
            elif block.get("type") == "tool_result":
                tool_use_id = str(block.get("tool_use_id"))
                self.results[tool_use_id] = bool(block.get("is_error"))
                if task_id:
                    self.background[str(task_id)] = tool_use_id


# --------------------------------------------------------------------------- redirects


def redirect_target(command: str, cwd: str | Path | None = None) -> Path | None:
    """Return the existing log file a shell command writes its output to, if any.

    Agents often run ``pytest > run.log 2>&1``; the task's own output file then stays
    empty and the interesting output is in ``run.log``. File descriptors (``2>&1``),
    ``/dev/*`` and paths that do not exist are ignored. Relative paths are resolved
    against ``cwd`` when given.
    """
    variables = _assignments(command)
    for match in _REDIRECT_RE.finditer(command):
        target = _expand(match.group(1), variables)
        if target.startswith("&") or target.startswith("/dev/"):
            continue
        path = Path(os.path.expanduser(target))
        if not path.is_absolute() and cwd:
            path = Path(cwd) / path
        if path.is_file():
            return path
    return None


def _expand(text: str, variables: dict[str, str]) -> str:
    """Expand ``$NAME`` and ``${NAME}`` from ``variables``, then the environment."""

    def value(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return variables.get(name, os.environ.get(name, match.group(0)))

    return _VARIABLE_RE.sub(value, text)


def _assignments(command: str) -> dict[str, str]:
    """Shell variables assigned in ``command`` (``S=/tmp/x``, ``export LOG="$S/a.log"``)."""
    variables: dict[str, str] = {}
    for name, raw in _ASSIGNMENT_RE.findall(command):
        if raw[:1] == "'":
            variables[name] = raw[1:-1]
        else:
            variables[name] = _expand(raw.strip('"'), variables)
    return variables


# --------------------------------------------------------------------------- model


@dataclass(eq=False)
class Task:
    """A background task launched by Claude Code."""

    id: str
    output: Path
    session: Session = field(repr=False)
    mtime: float
    size: int
    description: str = ""
    command: str = ""
    status: str = RUNNING
    #: Set for foreground commands (see :class:`TaskInfo`).
    tool_use_id: str = ""

    @property
    def foreground(self) -> bool:
        """Whether the agent is waiting for this command (not run in the background)."""
        return bool(self.tool_use_id)

    @property
    def finished(self) -> bool:
        """Whether Claude Code has reported the task as ended."""
        return self.status != RUNNING

    @property
    def label(self) -> str:
        """Human-readable name: description, else the start of the command."""
        command = " ".join(self.command.split())  # one line, even for multi-line scripts
        return self.description or command[:70] or "(unknown command)"

    @property
    def log_file(self) -> Path:
        """File holding the task's output: a redirect target if any, else the task output."""
        return redirect_target(self.command, self.session.cwd or None) or self.output

    def current_status(self) -> str:
        """Re-read the transcript and return this task's latest status."""
        return parse_transcript(self.session.transcript).status_of(self.id, self.tool_use_id)


@dataclass(eq=False)
class Session:
    """A Claude Code conversation that has at least one background task."""

    project: str
    id: str
    outputs: list[Path]
    title: str = ""
    cwd: str = ""
    tasks: list[Task] = field(default_factory=list)

    @property
    def transcript(self) -> Path:
        """Path of the conversation's JSON-lines transcript."""
        return projects_dir() / self.project / f"{self.id}.jsonl"

    @property
    def label(self) -> str:
        """Conversation title, else a shortened session id."""
        return self.title or f"session {self.id[:8]}"

    @property
    def mtime(self) -> float:
        """Time of the most recent task output change."""
        return max((task.mtime for task in self.tasks), default=0.0)

    @property
    def running(self) -> list[Task]:
        """Tasks not yet reported as ended."""
        return [task for task in self.tasks if not task.finished]

    def refresh(self) -> Session:
        """(Re)load title, cwd and tasks from disk. Returns ``self`` for chaining."""
        info = parse_transcript(self.transcript)
        self.title, self.cwd = info.title, info.cwd
        tasks = []
        for output in self.outputs:
            try:
                stat = output.stat()
            except OSError:  # removed since it was listed
                continue
            meta = info.tasks.get(output.stem, TaskInfo())
            tasks.append(
                Task(
                    id=output.stem,
                    output=output,
                    session=self,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    description=meta.description,
                    command=meta.command,
                    status=meta.status,
                )
            )
        self.tasks = sorted(tasks, key=lambda task: task.mtime)
        self._claim_foreground(info)
        return self

    def _claim_foreground(self, info: TranscriptInfo) -> None:
        """Attach running foreground commands to the outputs no background task claims.

        While a foreground command runs, Claude Code streams it to a task output file but
        the transcript only holds the tool call, without a task id. Pair the newest
        unclaimed outputs with the newest pending calls.
        """
        unclaimed = [task for task in self.tasks if task.id not in info.tasks]
        for task, call in zip(reversed(unclaimed), reversed(info.pending), strict=False):
            task.description = call.description
            task.command = call.command
            task.tool_use_id = call.tool_use_id

    def matches(self, query: str) -> bool:
        """Tell whether ``query`` is a prefix of the id or a substring of title or cwd."""
        q = query.lower()
        return self.id.startswith(q) or q in self.title.lower() or q in self.cwd.lower()


def discover(here: Path | None = None, session_query: str | None = None) -> list[Session]:
    """Return conversations with background tasks, oldest activity first.

    :param here: keep only conversations whose project is ``here`` or one of its parents.
    :param session_query: keep only conversations matching :meth:`Session.matches`.
    """
    groups: dict[tuple[str, str], list[Path]] = {}
    for output in tmp_root().glob("*/*/tasks/*.output"):
        session_dir = output.parent.parent
        groups.setdefault((session_dir.parent.name, session_dir.name), []).append(output)

    sessions = [
        Session(project, sid, outputs).refresh() for (project, sid), outputs in groups.items()
    ]
    sessions = [s for s in sessions if s.tasks]
    if here is not None:
        resolved = here.resolve()
        slugs = {project_slug(d) for d in (resolved, *resolved.parents)}
        sessions = [s for s in sessions if s.project in slugs]
    if session_query:
        sessions = [s for s in sessions if s.matches(session_query)]
    return sorted(sessions, key=lambda s: s.mtime)


def all_tasks(sessions: Iterable[Session]) -> list[Task]:
    """Every task of ``sessions``, oldest first."""
    return sorted((t for s in sessions for t in s.tasks), key=lambda task: task.mtime)
