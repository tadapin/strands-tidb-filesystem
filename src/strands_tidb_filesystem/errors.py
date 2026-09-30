"""Error types raised by the TiDB Cloud Filesystem integration."""

from __future__ import annotations


class TiFSError(RuntimeError):
    """A ``ti fs`` command failed.

    Attributes:
        command: The ``ti`` subcommand that failed (without credentials).
        exit_code: Process exit code.
        code: Machine-readable error code reported by ``ti``, if any.
        message: Human-readable error message.
    """

    def __init__(self, message: str, *, command: str = "", exit_code: int | None = None, code: str | None = None):
        """Initialize the error."""
        super().__init__(message)
        self.message = message
        self.command = command
        self.exit_code = exit_code
        self.code = code

    def __str__(self) -> str:
        """Render the error with its command and code for logs."""
        parts = [self.message]
        if self.code:
            parts.append(f"code={self.code}")
        if self.command:
            parts.append(f"command=ti {self.command}")
        return " | ".join(parts)


class TiNotInstalledError(TiFSError):
    """The ``ti`` CLI could not be found."""


class TiFSNotFoundError(TiFSError, FileNotFoundError):
    """The remote path does not exist."""


class TiFSPermissionError(TiFSError, PermissionError):
    """The token is not allowed to perform the operation."""


class TiFSAuthError(TiFSError, PermissionError):
    """The token is missing, malformed, expired or revoked."""


class TiFSQuotaError(TiFSError):
    """A storage, usage or file-size quota was exceeded."""


class TiFSConflictError(TiFSError):
    """The operation conflicts with the current remote state (e.g. directory not empty)."""


class TiFSTransientError(TiFSError):
    """A temporary backend or network failure; retrying may succeed."""


class TiVersionMismatchError(TiFSError):
    """An installed ``ti`` is not the version that was requested."""
