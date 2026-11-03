"""Command-line interface: list, follow and print Claude Code background tasks."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO, TextIO, cast

from . import __version__
from .discovery import COMPLETED, Session, Task, all_tasks, discover, tmp_root

EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_NOT_FOUND = 2
EXIT_INTERRUPTED = 130

STATUS_COLORS = {"running": "33", "completed": "32", "failed": "31", "killed": "35"}

Picker = Callable[[list[Task]], "Task | None"]


class NotFoundError(Exception):
    """No conversation or task matches the user's query."""


# --------------------------------------------------------------------------- formatting


class Style:
    """ANSI styling, disabled when not writing to a terminal or when ``NO_COLOR`` is set."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    @classmethod
    def for_stream(cls, stream: TextIO) -> Style:
        """Style for ``stream``: coloured only if it is a terminal and NO_COLOR is unset."""
        return cls(stream.isatty() and not os.environ.get("NO_COLOR"))

    def __call__(self, text: str, code: str) -> str:
        """Wrap ``text`` in the ANSI SGR ``code`` (e.g. ``"1;36"``)."""
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def status(self, status: str, width: int = 0) -> str:
        """Task status padded to ``width`` and coloured by its value."""
        return self(f"{status:<{width}}", STATUS_COLORS.get(status, "0"))


def format_age(seconds: float) -> str:
    """Compact age: ``42s``, ``7m``, ``3h05``, ``2d``."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}"
    return f"{s // 86400}d"


def format_size(n: float) -> str:
    """Compact byte size: ``512B``, ``41KB``, ``1.5GB``."""
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def task_line(task: Task, style: Style, now: float, with_session: bool = False) -> str:
    """One-line summary of a task."""
    line = (
        f"{style(task.id, '1')}  {style.status(task.status, 9)} "
        f"{format_age(now - task.mtime):>6} ago  {format_size(task.size):>6}  {task.label}"
    )
    if task.foreground:
        line += style("  (foreground)", "2")
    if with_session:
        line += style(f"  [{task.session.label}]", "2")
    return line


def render_sessions(sessions: Sequence[Session], limit: int, style: Style, now: float) -> str:
    """Conversations (newest last) with their most recent tasks."""
    shown = list(sessions[-limit:]) if limit > 0 else list(sessions)
    lines: list[str] = []
    for session in shown:
        head = style(f"● {session.label}", "1;36")
        if session.running:
            head += style(f"  ({len(session.running)} running)", "33")
        lines.append(head)
        lines.append(style(f"  {session.cwd or session.project}  ·  {session.id}", "2"))
        lines.extend("  " + task_line(t, style, now) for t in session.tasks[-10:])
        lines.append("")
    hidden = len(sessions) - len(shown)
    if hidden:
        lines.append(style(f"({hidden} older conversations hidden; use -l 0 to show all)", "2"))
    return "\n".join(lines).rstrip("\n") + "\n"


def render_header(task: Task, style: Style) -> str:
    """Header printed (to stderr) before a task's output."""
    session = task.session
    lines = [
        style(
            f"==> {task.id} [{task.status}] {task.description}", STATUS_COLORS.get(task.status, "1")
        ),
        style(f"    conversation: {session.label}  ({session.cwd or session.project})", "2"),
    ]
    if task.command:
        command = task.command if len(task.command) <= 500 else task.command[:500] + " ..."
        lines.append(style("    $ " + command.replace("\n", "\n      "), "2"))
    lines.append(style(f"    {task.log_file}", "2"))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- selection


def task_matches(task: Task, query: str) -> bool:
    """Exact id, or case-insensitive substring of id, description, command or title."""
    if task.id == query:
        return True
    q = query.lower()
    haystacks = (task.id, task.description, task.command, task.session.title)
    return any(q in h.lower() for h in haystacks)


def fzf_picker(tasks: list[Task]) -> Task | None:
    """Let the user choose a task with fzf. ``None`` if fzf or a terminal is unavailable."""
    if not (sys.stdin.isatty() and sys.stderr.isatty() and shutil.which("fzf")):
        return None
    now = time.time()
    style = Style(True)
    entries = [task_line(t, style, now, with_session=True) for t in reversed(tasks)]
    preview = f"{shlex.quote(sys.executable)} -m claude_report -p {{1}} 2>/dev/null | tail -n 60"
    result = subprocess.run(
        ["fzf", "--ansi", "--no-sort", "--prompt", "task> ", "--preview", preview],
        input="\n".join(entries),
        text=True,
        stdout=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise KeyboardInterrupt
    chosen = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout).split()[0]
    return next(t for t in tasks if t.id == chosen)


def select_task(
    sessions: Sequence[Session],
    query: str = "",
    *,
    pick: bool = False,
    picker: Picker = fzf_picker,
) -> Task:
    """Choose the task a command should act on.

    Candidates are all tasks matching ``query`` (every task if empty). With ``pick``
    the user chooses among all candidates. Otherwise a single running candidate wins;
    with several running ones the user chooses (newest if no picker is available);
    with none running, the newest candidate wins.
    """
    tasks = all_tasks(sessions)
    if query:
        tasks = [t for t in tasks if task_matches(t, query)]
    if not tasks:
        raise NotFoundError(f"no task matching {query!r}" if query else "no background task found")
    if pick:
        return picker(tasks) or tasks[-1]
    running = [t for t in tasks if not t.finished]
    if len(running) > 1:
        return picker(running) or running[-1]
    return (running or tasks)[-1]


# --------------------------------------------------------------------------- output


def tail_bytes(data: bytes, lines: int) -> bytes:
    """Last ``lines`` lines of ``data``; everything if ``lines`` is negative."""
    if lines < 0:
        return data
    if lines == 0:
        return b""
    return b"".join(data.splitlines(keepends=True)[-lines:])


def follow(
    task: Task,
    *,
    lines: int = 40,
    out: BinaryIO,
    err: TextIO,
    style: Style,
    poll_interval: float = 0.25,
    status_interval: float = 2.0,
) -> int:
    """Stream a task's output until the task ends, like ``tail -f`` that knows when to stop.

    Returns :data:`EXIT_OK` if the task completed, :data:`EXIT_TASK_FAILED` otherwise.
    """
    err.write(render_header(task, style))
    err.flush()
    with open(task.log_file, "rb") as handle:
        out.write(tail_bytes(handle.read(), lines))
        out.flush()
        status = task.status
        last_check = time.monotonic()
        while status == "running":
            chunk = handle.read()
            if chunk:
                out.write(chunk)
                out.flush()
                continue
            if time.monotonic() - last_check >= status_interval:
                last_check = time.monotonic()
                status = task.current_status()
                continue
            time.sleep(poll_interval)
        out.write(handle.read())  # anything written between the last read and the end
        out.flush()
    err.write(style(f"\n==> {task.id} {status}\n", STATUS_COLORS.get(status, "1")))
    return EXIT_OK if status == COMPLETED else EXIT_TASK_FAILED


# --------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    """Command-line parser."""
    parser = argparse.ArgumentParser(
        prog="claude-report",
        description="List and follow the background tasks (builds, test runs...) of your "
        "Claude Code conversations.",
        epilog="QUERY matches a task id, or part of a task's description, command or "
        "conversation title. --follow exits 0 if the task completed and 1 otherwise.",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "-f",
        "--follow",
        nargs="?",
        const="",
        metavar="QUERY",
        help="stream a task's output until it ends (default: the running task)",
    )
    action.add_argument(
        "--pick",
        nargs="?",
        const="",
        metavar="QUERY",
        help="choose a task interactively with fzf, then follow it",
    )
    action.add_argument("-p", "--print", metavar="QUERY", help="print a task's whole output")
    action.add_argument("--path", metavar="QUERY", help="print the path of a task's log file")
    parser.add_argument(
        "-s",
        "--session",
        metavar="QUERY",
        help="only conversations whose id starts with, or title/directory contains, QUERY",
    )
    parser.add_argument(
        "--here",
        action="store_true",
        help="only conversations started in the current directory or a parent",
    )
    parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=40,
        metavar="N",
        help="lines of existing output shown when following; -1 for all (default: 40)",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=5,
        metavar="N",
        help="number of conversations listed; 0 for all (default: 5)",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(  # noqa: PLR0911 - one return per action keeps dispatch flat
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    picker: Picker = fzf_picker,
) -> int:
    """Run the command line; returns the process exit code."""
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        sessions = discover(Path.cwd() if args.here else None, args.session)
        if not sessions:
            raise NotFoundError(f"no conversation with background tasks under {tmp_root()}")
        if args.follow is not None or args.pick is not None:
            query = args.pick if args.pick is not None else args.follow
            task = select_task(sessions, query, pick=args.pick is not None, picker=picker)
            return follow(
                task, lines=args.lines, out=_binary(out), err=err, style=Style.for_stream(err)
            )
        if args.print:
            task = select_task(sessions, args.print, picker=picker)
            err.write(render_header(task, Style.for_stream(err)))
            _binary(out).write(task.log_file.read_bytes())
            return EXIT_OK
        if args.path:
            out.write(f"{select_task(sessions, args.path, picker=picker).log_file}\n")
            return EXIT_OK
        out.write(render_sessions(sessions, args.limit, Style.for_stream(out), time.time()))
        return EXIT_OK
    except NotFoundError as exc:
        err.write(f"claude-report: {exc}\n")
        return EXIT_NOT_FOUND
    except KeyboardInterrupt:
        err.write("\n")
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        return EXIT_OK


def main_entry() -> None:
    """Console-script entry point."""
    sys.exit(main())


def _binary(stream: TextIO) -> BinaryIO:
    """Underlying byte stream of a text stream (task output may not be valid UTF-8)."""
    stream.flush()
    buffer = getattr(stream, "buffer", None)
    return buffer if buffer is not None else cast(BinaryIO, _TextAsBinary(stream))


class _TextAsBinary:
    """Minimal binary adapter for text streams without ``.buffer`` (e.g. StringIO)."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def write(self, data: bytes) -> int:
        self._stream.write(data.decode("utf-8", errors="replace"))
        return len(data)

    def flush(self) -> None:
        self._stream.flush()
