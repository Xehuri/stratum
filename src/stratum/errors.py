"""Shared error types for ingest and contract validation.

The CLI maps these to the exit codes from the design contract:
``BusinessError`` is a business failure (1) and ``UsageProblem`` is an
argument or environment error (2).
"""

from __future__ import annotations


class StratumError(Exception):
    """Base class for errors that the CLI renders as plain text."""

    exit_code = 1


class BusinessError(StratumError):
    """A business rule failed; the command did not change the database."""

    exit_code = 1


class UsageProblem(StratumError):
    """The caller supplied an unusable argument or environment."""

    exit_code = 2


class ValidationError(BusinessError):
    """A contract validation failure with a stable lint code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
