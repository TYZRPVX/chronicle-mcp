"""Synthetic-only regression tests for the local, read-only MCP workflow."""

import asyncio
import importlib
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from chronicle_mcp import config
from chronicle_mcp.connection import get_history_connection
from chronicle_mcp.core import HistoryService
from chronicle_mcp.core.exceptions import DatabaseLockedError, WritesDisabledError
from chronicle_mcp.database import query_history
from chronicle_mcp.database.utils import sanitize_url

adapter = importlib.import_module("chronicle_mcp.protocols.mcp")

RAW_URL = "https://example.test/live-config?token=synthetic-secret#route"


@pytest.fixture
def history(tmp_path, monkeypatch):
    path = tmp_path / "History #1"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE urls (id INTEGER PRIMARY KEY, title TEXT, url TEXT, last_visit_time INTEGER, visit_count INTEGER)"
        )
        db.executemany(
            "INSERT INTO urls VALUES (?, ?, ?, ?, ?)",
            [
                (1, "Live Configuration", RAW_URL, 13300000000000000, 1),
                (2, "Live dashboard", "https://example.test/config", 13300000000000001, 1),
                (3, "Unrelated", "https://example.test/other", 13300000000000002, 1),
            ],
        )
    monkeypatch.setattr("chronicle_mcp.connection.get_browser_path", lambda browser: str(path))
    settings = tmp_path / "settings.toml"
    settings.write_text('[default]\nbrowser="vivaldi"\n[security]\nsanitize_urls=false\n')
    monkeypatch.setenv("CHRONICLE_CONFIG", str(settings))
    monkeypatch.delenv("CHRONICLE_BROWSER", raising=False)
    return path


def test_search_defaults_raw_url_and_no_sensitive_logs(history, caplog):
    caplog.set_level(logging.DEBUG)
    result = json.loads(adapter.search_history("live config"))
    assert result["success"]
    assert [row[0] for row in result["data"]] == ["Live dashboard", "Live Configuration"]
    assert result["data"][1][1] == RAW_URL
    adapter.search_history("synthetic-secret")
    assert "synthetic-secret" not in caplog.text
    assert "live config" not in caplog.text
    assert RAW_URL not in caplog.text


def test_browser_config_and_explicit_override(history, monkeypatch):
    seen = []
    monkeypatch.setattr(
        HistoryService, "search_history", lambda **kw: seen.append(kw["browser"]) or {"results": []}
    )
    adapter.search_history("live")
    adapter.search_history("live", browser="firefox")
    adapter.search_history("live", 5, "edge")
    monkeypatch.setenv("CHRONICLE_BROWSER", "brave")
    adapter.search_history("live")
    assert seen == ["vivaldi", "firefox", "edge", "brave"]


def test_sanitize_setting_and_custom_parameters(history):
    assert sanitize_url(RAW_URL) == RAW_URL
    settings = config.load_config()
    settings.security.sanitize_urls = True
    settings.security.sensitive_params = ["token"]
    assert sanitize_url(RAW_URL) == "https://example.test/live-config#route"
    result = json.loads(adapter.search_history("live config"))
    assert "synthetic-secret" not in json.dumps(result)


def test_readonly_no_copy_and_connection_closed(history, monkeypatch):
    monkeypatch.setattr("shutil.copy2", lambda *a, **k: pytest.fail("must not copy history"))
    with get_history_connection("vivaldi") as db:
        assert Path(db.execute("PRAGMA database_list").fetchone()[2]) == history
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 1000
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM urls")
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")
    with sqlite3.connect(history) as check:
        assert check.execute("SELECT count(*) FROM urls").fetchone()[0] == 3


def test_wal_changes_visible(history):
    writer = sqlite3.connect(history)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "INSERT INTO urls VALUES (4, 'Live new config', 'https://example.test/new', 13300000000000003, 1)"
        )
        writer.commit()
        with get_history_connection("vivaldi") as db:
            assert query_history(db, "new config")[0][0] == "Live new config"
    finally:
        writer.close()


def test_exclusive_lock_uses_private_snapshot(history):
    writer = sqlite3.connect(history)
    try:
        writer.execute("PRAGMA locking_mode=EXCLUSIVE")
        writer.execute("UPDATE urls SET visit_count=2")
        writer.commit()
        with get_history_connection("vivaldi") as db:
            snapshot = Path(db.execute("PRAGMA database_list").fetchone()[2])
            assert snapshot != history
            assert snapshot.stat().st_mode & 0o777 == 0o600
            assert snapshot.parent.stat().st_mode & 0o777 == 0o700
            assert len(query_history(db, "live config")) == 2
            with pytest.raises(sqlite3.OperationalError):
                db.execute("DELETE FROM urls")
        assert not snapshot.exists()
        with pytest.raises(ValueError):
            with get_history_connection("vivaldi") as db:
                snapshot = Path(db.execute("PRAGMA database_list").fetchone()[2])
                raise ValueError("caller failure")
        assert not snapshot.exists()
    finally:
        writer.close()


def test_locked_wal_does_not_ignore_wal(history):
    writer = sqlite3.connect(history)
    try:
        writer.execute("PRAGMA locking_mode=EXCLUSIVE")
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE urls SET visit_count=2")
        writer.commit()
        with pytest.raises(DatabaseLockedError):
            with get_history_connection("vivaldi"):
                pytest.fail("must not ignore WAL")
    finally:
        writer.close()


def test_active_journal_fails_closed(history, monkeypatch):
    writer = sqlite3.connect(history)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        journal = Path(str(history) + "-journal")
        journal.write_bytes(b"active!!")
        with pytest.raises(DatabaseLockedError):
            with get_history_connection("vivaldi"):
                pytest.fail("must not read an active transaction")
    finally:
        writer.close()
        journal.unlink(missing_ok=True)


def test_changing_snapshot_is_rejected(history, monkeypatch):
    import shutil

    copy = shutil.copyfileobj

    def changing_copy(src, dst, **kwargs):
        copy(src, dst, **kwargs)
        # Change metadata after every copy without changing database contents.
        stat = history.stat()
        os.utime(history, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))

    monkeypatch.setattr(shutil, "copyfileobj", changing_copy)
    writer = sqlite3.connect(history)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(DatabaseLockedError):
            with get_history_connection("vivaldi"):
                pytest.fail("must reject a changing source")
    finally:
        writer.close()


@pytest.mark.parametrize(
    "operation",
    [
        lambda: HistoryService.delete_history("live", confirm=True),
        lambda: HistoryService.delete_duplicates("vivaldi", confirm=True),
        lambda: HistoryService.sync_history("vivaldi", "chrome", dry_run=False),
    ],
)
def test_writes_blocked_before_database_access(monkeypatch, operation):
    monkeypatch.setattr(
        "chronicle_mcp.connection.get_browser_path",
        lambda *a: pytest.fail("must reject before opening"),
    )
    with pytest.raises(WritesDisabledError):
        operation()


def test_preview_and_parameterized_query(history):
    assert HistoryService.delete_history("live", browser="vivaldi")["preview"]
    with get_history_connection("vivaldi") as db:
        assert query_history(db, "' OR 1=1 --") == []
        assert len(query_history(db, "live config", 1)) == 1


@pytest.mark.parametrize("module", ["chronicle_mcp", "chronicle_mcp.server"])
def test_stdio_protocol_with_synthetic_database(history, module, tmp_path):
    # Deny network activity in the child and redirect every browser to synthetic data.
    bootstrap = """import socket, runpy, sys
from pathlib import Path
history_path = sys.argv[1]
allowed_databases = {history_path, Path(history_path).resolve().as_uri() + "?mode=ro", ":memory:"}
def audit(event, args):
    if event == "sqlite3.connect" and str(args[0]) not in allowed_databases:
        raise AssertionError("Only synthetic databases are allowed")
sys.addaudithook(audit)

def deny_network(*args, **kwargs):
    raise AssertionError("Network activity is forbidden in stdio test")
socket.socket.connect = deny_network
socket.socket.bind = deny_network
from chronicle_mcp import paths, connection
paths.get_browser_path = lambda browser: history_path
connection.get_browser_path = paths.get_browser_path
sys.argv = [sys.argv[2], "mcp"]
runpy.run_module(sys.argv[0], run_name="__main__")
"""

    async def run():
        stderr_path = tmp_path / "stderr.txt"
        with stderr_path.open("wb") as err:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                bootstrap,
                str(history),
                module,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=err,
                env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
            )

            async def request(message):
                proc.stdin.write((json.dumps(message) + "\n").encode())
                await proc.stdin.drain()
                if "id" not in message:
                    return None
                while True:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
                    assert line, stderr_path.read_text()
                    response = json.loads(line)  # Any stdout banner fails this check.
                    if response.get("id") == message["id"]:
                        return response

            try:
                await request(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "synthetic-test", "version": "1"},
                        },
                    }
                )
                await request({"jsonrpc": "2.0", "method": "notifications/initialized"})
                listed = await request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                names = {tool["name"] for tool in listed["result"]["tools"]}
                assert {"search_history", "sync_history", "delete_history"} <= names
                response = await request(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "search_history", "arguments": {"query": "live config"}},
                    }
                )
                envelope = json.loads(response["result"]["content"][0]["text"])
                assert envelope["success"], envelope
                assert envelope["data"][1][1] == RAW_URL
                response = await request(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "delete_history",
                            "arguments": {"query": "live", "confirm": True},
                        },
                    }
                )
                envelope = json.loads(response["result"]["content"][0]["text"])
                assert envelope["error"]["code"] == "WRITES_DISABLED"
            finally:
                proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
        logs = stderr_path.read_text()
        assert "synthetic-secret" not in logs
        assert "Network activity is forbidden" not in logs

    asyncio.run(run())
