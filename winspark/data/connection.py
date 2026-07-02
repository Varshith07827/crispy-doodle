"""Port of WinSpark.Infrastructure.Data.SqliteConnectionFactory + DatabaseInitializer."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from winspark.constants import APPLICATION_NAME, DATABASE_FILE_NAME
from winspark.data.schema import COLUMN_MIGRATIONS, STATEMENTS


def default_database_path() -> Path:
    """Mirrors Environment.SpecialFolder.LocalApplicationData / winSpark / winspark.db."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        # Non-Windows fallback (e.g. running this port's tests on macOS/Linux).
        local_app_data = str(Path.home() / ".local" / "share")
    return Path(local_app_data) / APPLICATION_NAME / DATABASE_FILE_NAME


class ConnectionFactory:
    """Opens SQLite connections with the same pragmas as the .NET SqliteConnectionFactory."""

    def __init__(self, database_path: str | Path):
        self._path = Path(database_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._wal_initialized = False

    def create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        self._apply_connection_pragmas(conn)
        return conn

    def _apply_connection_pragmas(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA foreign_keys = ON")
        if not self._wal_initialized:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA synchronous = NORMAL")
            self._wal_initialized = True

    def initialize_schema(self) -> None:
        """Port of DatabaseInitializer.InitializeAsync — creates tables/indexes if
        missing, then applies additive column migrations for older databases."""
        conn = self.create_connection()
        try:
            for statement in STATEMENTS:
                conn.execute(statement)
            self._apply_column_migrations(conn)
        finally:
            conn.close()

    @staticmethod
    def _apply_column_migrations(conn: sqlite3.Connection) -> None:
        for table, column, definition in COLUMN_MIGRATIONS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
