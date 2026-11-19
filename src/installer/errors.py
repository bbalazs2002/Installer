"""Shared exit codes and the base exception type."""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USER_ABORT = 2
EXIT_MISSING_PREREQUISITE = 3


class InstallerError(Exception):
    """Base class for all expected, user-presentable errors.

    The message is shown to the user as-is (in Hungarian).
    """


class UserAbort(InstallerError):
    """The user declined or cancelled an operation (exit code 2)."""


class PrerequisiteError(InstallerError):
    """A required tool or condition is missing (exit code 3)."""
