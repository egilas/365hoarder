"""TTY-aware colored console output shared by the 365hoarder tools."""

from __future__ import annotations

import sys
from typing import TextIO

from termcolor import colored


def _print(message: str, color: str, file: TextIO | None = None) -> None:
    print(colored(message, color), file=file or sys.stdout, flush=True)


def info(message: str, file: TextIO | None = None) -> None:
    _print(f"[*] {message}", "cyan", file)


def success(message: str, file: TextIO | None = None) -> None:
    _print(f"[+] {message}", "green", file)


def warning(message: str, file: TextIO | None = None) -> None:
    _print(f"Warning: {message}", "yellow", file or sys.stderr)


def error(message: str, file: TextIO | None = None) -> None:
    _print(f"Error: {message}", "red", file or sys.stderr)


def accent(message: str, file: TextIO | None = None) -> None:
    _print(message, "yellow", file)
