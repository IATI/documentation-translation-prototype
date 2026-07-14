"""Run logging: mirror everything printed to the terminal into a log file.

The log file gives the user something concrete to send to the tool's developer
when a run goes wrong, without having to copy-paste from a scrolling terminal.
"""

import sys
from pathlib import Path


class _Tee:
    """A write-through stream that forwards to the terminal and a log file."""

    def __init__(self, terminal, log_file):
        self._terminal = terminal
        self._log_file = log_file

    def write(self, text: str) -> int:
        self._terminal.write(text)
        self._terminal.flush()
        self._log_file.write(text)
        self._log_file.flush()
        return len(text)

    def flush(self) -> None:
        self._terminal.flush()
        self._log_file.flush()

    def isatty(self) -> bool:
        return getattr(self._terminal, "isatty", lambda: False)()


class RunLog:
    """Tee stdout to a log file for the duration of a run.

    Usage:
        runlog = RunLog(path)
        try:
            ...                       # all print() output is mirrored to the file
        finally:
            runlog.close()

    Use ``log_only()`` to write detail (e.g. a full traceback) to the file
    without showing it on the terminal.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file = open(self.path, "w", encoding="utf-8")
        # Tee both streams: stderr carries subprocess output and stray
        # tracebacks that the user would otherwise not have in the log they
        # send to the developer.
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = _Tee(self._orig_stdout, self._file)
        sys.stderr = _Tee(self._orig_stderr, self._file)

    def log_only(self, text: str) -> None:
        """Write text to the log file only, not the terminal."""
        self._file.write(text)
        self._file.flush()

    def close(self) -> None:
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        self._file.close()
