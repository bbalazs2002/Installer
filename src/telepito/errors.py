"""Shared exit codes and the base exception type."""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USER_ABORT = 2
EXIT_MISSING_PREREQUISITE = 3


class TelepitoError(Exception):
    """Base class for all expected, user-presentable errors.

    The message is shown to the user as-is (in Hungarian).
    """
