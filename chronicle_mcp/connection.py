"""Database connection management for ChronicleMCP.

This module provides a centralized way to connect to browser history databases
using read-only SQLite connections and a bounded lock timeout.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chronicle_mcp.core.exceptions import (
        BrowserNotFoundError,
        BrowserPathNotFoundError,
        DatabaseLockedError,
        PermissionDeniedError,
    )

from chronicle_mcp.paths import get_browser_path

logger = logging.getLogger(__name__)


class ConnectionError(Exception):
    """Base exception for connection errors."""

    def __init__(self, message: str, browser: str, details: str | None = None):
        self.message = message
        self.browser = browser
        self.details = details
        super().__init__(self.message)


def get_temp_filename(browser: str) -> str:
    """Generate a unique temporary filename for the database copy.

    Args:
        browser: Browser name for identification

    Returns:
        Path string for the temporary file
    """
    temp_dir = tempfile.gettempdir()
    return os.path.join(
        temp_dir,
        f"chronicle_{browser}_temp_{os.getpid()}_{int(time.time() * 1000)}.db",
    )


def cleanup_temp_file(temp_path: str) -> None:
    """Safely remove a temporary database file.

    Args:
        temp_path: Path to the temporary file to remove
    """
    try:
        if os.path.exists(temp_path):
            os.remove(temp_path)
            logger.debug(f"Cleaned up temp file: {temp_path}")
    except OSError as e:
        logger.warning(f"Failed to clean up temp file {temp_path}: {e}")


def open_readonly_database(path: str) -> sqlite3.Connection:
    """Open an existing database without copying it; respect SQLite WAL state."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        conn.execute("PRAGMA query_only = ON")
    except Exception:
        conn.close()
        raise
    return conn


def _file_state(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except FileNotFoundError:
        return None


def _inactive_journal(path: Path) -> bool:
    try:
        with path.open("rb") as journal:
            header = journal.read(8)
        return not header or header == b"\0" * 8
    except FileNotFoundError:
        return True


@contextmanager
def _locked_history_snapshot(path: str) -> Generator[sqlite3.Connection, None, None]:
    """Best-effort stable rollback-mode snapshot; never ignore a WAL/hot journal.

    Chromium can retain an exclusive lock between transactions. Only copy during
    an observed quiet interval with an absent/invalidated rollback journal. This
    is not a transactional backup API; concurrent activity fails closed.
    """
    source = Path(path)
    journal = Path(path + "-journal")
    wal = Path(path + "-wal")
    files = (source, journal, wal)
    for _ in range(3):
        before = tuple(_file_state(file) for file in files)
        if before[0] is None or before[2] is not None or not _inactive_journal(journal):
            time.sleep(0.02)
            continue
        with tempfile.TemporaryDirectory(prefix="chronicle-snapshot-") as directory:
            target = Path(directory) / "History"
            # Exclusive creation in a private directory; never copy source permissions.
            with target.open("xb") as output:
                os.chmod(target, 0o600)
                with source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output, length=1024 * 1024)
            after = tuple(_file_state(file) for file in files)
            if before != after or not _inactive_journal(journal):
                continue
            conn = open_readonly_database(str(target))
            try:
                if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise sqlite3.OperationalError("Snapshot integrity check failed")
                yield conn
                return
            finally:
                conn.close()
    raise sqlite3.OperationalError("History is busy; no stable snapshot available")


@contextmanager
def get_history_connection(
    browser: str = "chrome",
    max_retries: int = 3,
    retry_delay: float = 0.1,
) -> Generator[sqlite3.Connection, None, None]:
    """Read history directly, or use a private stable snapshot on an exclusive lock.

    max_retries and retry_delay remain accepted for API compatibility, but are
    ignored: a context manager must never retry the caller's operation.
    """
    from chronicle_mcp.core.exceptions import (
        BrowserNotFoundError,
        BrowserPathNotFoundError,
        DatabaseLockedError,
        PermissionDeniedError,
    )

    browser_lower = browser.lower()
    history_path = get_browser_path(browser_lower)
    if not history_path:
        raise BrowserNotFoundError(browser_lower)
    if not os.path.exists(history_path):
        raise BrowserPathNotFoundError(browser_lower, history_path)

    conn = None
    try:
        conn = open_readonly_database(history_path)
        # Detect retained exclusive locks before yielding; never retry caller code.
        conn.execute("PRAGMA busy_timeout = 50")
        try:
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() and "busy" not in str(error).lower():
                raise
            conn.close()
            conn = None
            with _locked_history_snapshot(history_path) as snapshot:
                yield snapshot
            return
        conn.execute("PRAGMA busy_timeout = 1000")
        yield conn
    except PermissionError as e:
        raise PermissionDeniedError(browser_lower, history_path) from e
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            raise DatabaseLockedError(browser_lower) from e
        raise
    finally:
        if conn is not None:
            conn.close()


def execute_with_connection(browser: str, func: Callable[[sqlite3.Connection], Any]) -> Any:
    """Execute a function with a database connection.

    This is a convenience function for synchronous operations that need
    a database connection without using the context manager pattern.

    Args:
        browser: Browser name
        func: Function to execute with the connection

    Returns:
        Result of the function execution

    Raises:
        ConnectionError: Any connection-related error
    """
    with get_history_connection(browser) as conn:
        return func(conn)


# Re-export exceptions from core.exceptions for backward compatibility
from chronicle_mcp.core.exceptions import (
    BrowserNotFoundError,
    BrowserPathNotFoundError,
    DatabaseLockedError,
    PermissionDeniedError,
)

__all__ = [
    "BrowserNotFoundError",
    "BrowserPathNotFoundError",
    "DatabaseLockedError",
    "PermissionDeniedError",
    "ConnectionError",
    "get_history_connection",
    "execute_with_connection",
    "get_temp_filename",
    "cleanup_temp_file",
]
