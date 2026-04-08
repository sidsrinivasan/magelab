"""
SQLite connection manager and framework-level persistence for magelab runs.

Database is a thin connection manager that owns framework tables and exposes
connection primitives for stores. Each store owns its own DDL, write queries,
and load queries via register_schema() and the execute/fetch primitives.

Framework tables:
- run_meta — one row per run segment (timing, outcome, costs, full OrgConfig JSON)
- run_events — event lifecycle (enqueue → deliver → complete)
- run_transcripts — agent conversation logs
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Union

from ..events import EventOutcome
from ..runners.agent_runner import ERROR_API_ERROR, ERROR_API_OVERLOADED, ERROR_RATE_LIMITED

SCHEMA_VERSION = 1


class Database:
    """SQLite connection manager for a single magelab run.

    Owns framework-level tables (run_meta, run_events, run_transcripts).
    Store-specific tables are registered and managed by each store.

    All write methods are synchronous (sqlite3 is not async). Callers invoke
    them inside or immediately after the async store lock, so writes are
    serialized and sub-millisecond.

    Args:
        db_path: Path to the SQLite file. Created if it doesn't exist.
    """

    def __init__(self, db_path: Union[Path, str]) -> None:
        self._path = Path(db_path)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit mode — we manage transactions manually
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # =========================================================================
    # Connection primitives (used by stores)
    # =========================================================================

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a SQL statement and return the cursor."""
        return self._conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        """Execute a query and return the first row as a dict, or None."""
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a query and return all rows as dicts."""
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def commit(self) -> None:
        """Commit if not inside a transaction() block.

        Behavior depends on context:
        - **Outside transaction()**: With isolation_level=None (autocommit),
          SQLite auto-commits each statement immediately. This call is a
          harmless no-op — it's kept so callers don't need to know whether
          they're inside a transaction or not.
        - **Inside transaction()**: in_transaction is True, so we skip.
          The transaction() context manager issues the final COMMIT/ROLLBACK.

        This means individual persistence methods (e.g. _db_upsert_agent)
        can call commit() unconditionally — it does the right thing whether
        called standalone or composed inside a transaction() block.
        """
        if not self._conn.in_transaction:
            self._conn.commit()

    @contextmanager
    def transaction(self):
        """Context manager for grouping multiple writes into a single transaction.

        Individual write methods skip their auto-commit when inside this block.

        Usage:
            with db.transaction():
                db.execute("INSERT ...", (...))
                db.execute("INSERT ...", (...))
        """
        if self._conn.in_transaction:
            raise RuntimeError("transaction() is not re-entrant")
        self._conn.execute("BEGIN")
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # =========================================================================
    # Schema
    # =========================================================================

    def _create_schema(self) -> None:
        """Create framework-level tables.

        Store-specific tables are registered by each store via register_schema().
        """
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS run_meta (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version      INTEGER NOT NULL,
                org_name            TEXT NOT NULL,
                org_config          TEXT NOT NULL,
                resume_mode         TEXT,
                start_time          TEXT,
                end_time            TEXT,
                duration_seconds    REAL,
                total_cost_usd      REAL,
                timed_out           INTEGER NOT NULL DEFAULT 0,
                outcome             TEXT,
                sync_rounds         INTEGER,
                tasks_succeeded     INTEGER,
                tasks_failed        INTEGER,
                tasks_open          INTEGER,
                rate_limited_429    INTEGER NOT NULL DEFAULT 0,
                api_overloaded_529  INTEGER NOT NULL DEFAULT 0,
                api_error_other     INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS run_events (
                event_id         TEXT PRIMARY KEY,
                event_type       TEXT NOT NULL,
                target_agent_id  TEXT NOT NULL,
                source_agent_id  TEXT,
                task_id          TEXT,
                wire_id          TEXT,
                payload          TEXT,
                timestamp        TEXT NOT NULL,
                outcome          TEXT,
                num_turns        INTEGER,
                cost_usd         REAL,
                duration_ms      INTEGER,
                timed_out        INTEGER,
                error            TEXT,
                finished_at      TEXT
            );

            CREATE TABLE IF NOT EXISTS run_transcripts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id     TEXT NOT NULL,
                entry_type   TEXT NOT NULL,
                content      TEXT NOT NULL,
                timestamp    TEXT NOT NULL,
                turn_number  INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_run_events_outcome
                ON run_events (outcome);
            CREATE INDEX IF NOT EXISTS idx_run_events_target
                ON run_events (target_agent_id);
            CREATE INDEX IF NOT EXISTS idx_run_events_timestamp
                ON run_events (timestamp);
            CREATE INDEX IF NOT EXISTS idx_run_transcripts_agent
                ON run_transcripts (agent_id);
        """)

    def register_schema(self, ddl: str) -> None:
        """Register a schema extension. DDL must use IF NOT EXISTS for idempotency.

        Must be called outside any transaction (executescript issues an implicit COMMIT).
        """
        for stmt in ddl.split(";"):
            stmt_upper = stmt.strip().upper()
            if stmt_upper.startswith("CREATE") and "IF NOT EXISTS" not in stmt_upper:
                raise ValueError(f"Schema DDL statement must use IF NOT EXISTS for idempotency: {stmt.strip()[:80]}")
        if self._conn.in_transaction:
            raise RuntimeError("register_schema must not be called inside a transaction")
        self._conn.executescript(ddl)

    # =========================================================================
    # run_meta
    # =========================================================================

    def init_run_meta(
        self,
        org_name: str,
        org_config: str,
        start_time: Optional[str] = None,
        resume_mode: Optional[str] = None,
    ) -> None:
        """Append a new run segment row. Each fresh run or resume gets its own row."""
        ts = start_time or datetime.now(timezone.utc).isoformat()
        self.execute(
            "INSERT INTO run_meta (schema_version, org_name, org_config, resume_mode, start_time) VALUES (?, ?, ?, ?, ?)",
            (SCHEMA_VERSION, org_name, org_config, resume_mode, ts),
        )
        self.commit()

    def compute_run_summary(self) -> dict:
        """Aggregate run statistics from run_events.

        Returns a dict with: start_time, total_cost_usd, rate_limited_429,
        api_overloaded_529, api_error_other.
        """
        meta = self.fetchone("SELECT start_time FROM run_meta WHERE id = (SELECT MAX(id) FROM run_meta)")
        start_time = meta["start_time"] if meta else None

        row = self.fetchone("SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM run_events")
        total_cost_usd = row["total"]

        error_rows = self.fetchall("SELECT error FROM run_events WHERE error IS NOT NULL")
        rate_limited_429 = 0
        api_overloaded_529 = 0
        api_error_other = 0
        for r in error_rows:
            err = r["error"]
            if err.startswith(ERROR_RATE_LIMITED):
                rate_limited_429 += 1
            elif err.startswith(ERROR_API_OVERLOADED):
                api_overloaded_529 += 1
            elif err.startswith(ERROR_API_ERROR):
                api_error_other += 1

        return {
            "start_time": start_time,
            "total_cost_usd": total_cost_usd,
            "rate_limited_429": rate_limited_429,
            "api_overloaded_529": api_overloaded_529,
            "api_error_other": api_error_other,
        }

    def finalize_run(
        self,
        *,
        end_time: str,
        duration_seconds: Optional[float],
        timed_out: bool,
        outcome: str,
        sync_rounds: Optional[int] = None,
        total_cost_usd: float = 0.0,
        tasks_succeeded: int = 0,
        tasks_failed: int = 0,
        tasks_open: int = 0,
        rate_limited_429: int = 0,
        api_overloaded_529: int = 0,
        api_error_other: int = 0,
    ) -> None:
        """Write final run statistics for the latest segment."""
        self.execute(
            """UPDATE run_meta SET
                end_time = ?, duration_seconds = ?, total_cost_usd = ?, timed_out = ?,
                outcome = ?, sync_rounds = ?,
                tasks_succeeded = ?, tasks_failed = ?, tasks_open = ?,
                rate_limited_429 = ?, api_overloaded_529 = ?, api_error_other = ?
               WHERE id = (SELECT MAX(id) FROM run_meta)
            """,
            (
                end_time,
                duration_seconds,
                total_cost_usd,
                1 if timed_out else 0,
                outcome,
                sync_rounds,
                tasks_succeeded,
                tasks_failed,
                tasks_open,
                rate_limited_429,
                api_overloaded_529,
                api_error_other,
            ),
        )
        self.commit()

    def load_run_meta(self) -> Optional[dict]:
        """Load the latest run_meta row. Returns None if no run exists."""
        return self.fetchone("SELECT * FROM run_meta ORDER BY id DESC LIMIT 1")

    def load_all_run_segments(self) -> list[dict]:
        """Load all run segments in chronological order."""
        return self.fetchall("SELECT * FROM run_meta ORDER BY id")

    def run_count(self) -> int:
        """Return the total number of run segments in run_meta (including in-progress)."""
        row = self.fetchone("SELECT COUNT(*) AS n FROM run_meta")
        return row["n"] if row else 0

    def get_schema_version(self) -> Optional[int]:
        """Get the schema version from the latest run_meta row. Returns None if no run exists."""
        row = self.fetchone("SELECT schema_version FROM run_meta ORDER BY id DESC LIMIT 1")
        return row["schema_version"] if row else None

    # =========================================================================
    # run_events
    # =========================================================================

    def insert_event(
        self,
        event_id: str,
        event_type: str,
        target_agent_id: str,
        source_agent_id: Optional[str],
        task_id: Optional[str],
        wire_id: Optional[str],
        timestamp: str,
        payload: Optional[str] = None,
    ) -> None:
        """Insert an event at enqueue time (outcome=NULL).

        Args:
            event_id: Unique event identifier (short UUID from the Event).
            payload: JSON string of event-specific fields beyond BaseEvent.
        """
        self.execute(
            """INSERT INTO run_events (event_id, event_type, target_agent_id, source_agent_id, task_id, wire_id, payload, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, event_type, target_agent_id, source_agent_id, task_id, wire_id, payload, timestamp),
        )
        self.commit()

    def update_event_finished(
        self,
        event_id: str,
        *,
        num_turns: int,
        cost_usd: Optional[float],
        duration_ms: int,
        timed_out: bool,
        error: Optional[str],
        finished_at: str,
    ) -> None:
        """Record agent run results and mark event as completed."""
        self.execute(
            """UPDATE run_events SET
                outcome = ?, num_turns = ?, cost_usd = ?, duration_ms = ?, timed_out = ?,
                error = ?, finished_at = ?
               WHERE event_id = ?""",
            (
                EventOutcome.COMPLETED.value,
                num_turns,
                cost_usd,
                duration_ms,
                1 if timed_out else 0,
                error,
                finished_at,
                event_id,
            ),
        )
        self.commit()

    def update_event_outcome(self, event_id: str, outcome: EventOutcome) -> None:
        """Set outcome for a single event."""
        self.execute("UPDATE run_events SET outcome = ? WHERE event_id = ?", (outcome.value, event_id))
        self.commit()

    def update_events_by_outcome(self, where_outcome: Optional[EventOutcome], new_outcome: EventOutcome) -> int:
        """Batch-update all events matching a given outcome. None matches NULL (undelivered).

        Returns the number of events updated.
        """
        if where_outcome is None:
            cursor = self.execute(
                "UPDATE run_events SET outcome = ? WHERE outcome IS NULL",
                (new_outcome.value,),
            )
        else:
            cursor = self.execute(
                "UPDATE run_events SET outcome = ? WHERE outcome = ?",
                (new_outcome.value, where_outcome.value),
            )
        self.commit()
        return cursor.rowcount

    def load_undelivered_events(self) -> list[dict]:
        """Load events with outcome IS NULL, ordered by timestamp."""
        return self.fetchall("SELECT * FROM run_events WHERE outcome IS NULL ORDER BY timestamp")

    # =========================================================================
    # run_transcripts
    # =========================================================================

    def insert_transcript_entry(
        self,
        agent_id: str,
        entry_type: str,
        content: str,
        timestamp: str,
        turn_number: Optional[int] = None,
    ) -> None:
        """Insert a transcript entry."""
        self.execute(
            "INSERT INTO run_transcripts (agent_id, entry_type, content, timestamp, turn_number) VALUES (?, ?, ?, ?, ?)",
            (agent_id, entry_type, content, timestamp, turn_number),
        )
        self.commit()

    def create_transcript_listener(self) -> Callable[[str, str, str], None]:
        """Create a transcript listener that writes entries to the DB.

        Tracks per-agent turn numbers: system_prompt resets to 0,
        each prompt increments. Returns a callable suitable for
        TranscriptLogger.add_listener().
        """
        turn_counters: dict[str, int] = {}

        def listener(agent_id: str, entry_type: str, content: str) -> None:
            if entry_type == "system_prompt":
                turn_counters[agent_id] = 0
            elif entry_type == "prompt":
                turn_counters[agent_id] = turn_counters.get(agent_id, 0) + 1
            self.insert_transcript_entry(
                agent_id=agent_id,
                entry_type=entry_type,
                content=content,
                timestamp=datetime.now(timezone.utc).isoformat(),
                turn_number=turn_counters.get(agent_id, 0),
            )

        return listener

    def load_transcript_entries(self) -> list[dict]:
        """Load all transcript entries ordered by id.

        Returns a list of dicts with keys: agent_id, entry_type, content.
        """
        rows = self.fetchall("SELECT agent_id, entry_type, content FROM run_transcripts ORDER BY id")
        return [{"agent_id": r["agent_id"], "entry_type": r["entry_type"], "content": r["content"]} for r in rows]
