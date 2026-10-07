"""SQLite persistence; runtime data never belongs in version control."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def packed(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sources (
                key TEXT PRIMARY KEY, kind TEXT NOT NULL, external_id TEXT NOT NULL,
                note_id TEXT NOT NULL, parent_id TEXT, title TEXT, text TEXT NOT NULL,
                site TEXT NOT NULL, keywords TEXT NOT NULL DEFAULT '[]',
                published_at TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                collected_at TEXT, status TEXT NOT NULL DEFAULT 'pending',
                relevance TEXT NOT NULL DEFAULT 'unknown', review_status TEXT NOT NULL DEFAULT 'unreviewed',
                manual INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', duplicate_of TEXT,
                version INTEGER NOT NULL DEFAULT 1, fingerprint TEXT NOT NULL,
                analysis_fingerprint TEXT, analysis_text TEXT, analysis_version INTEGER,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, created_at TEXT NOT NULL,
                site TEXT NOT NULL, counts TEXT NOT NULL, errors TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batch_sources (
                batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                source_key TEXT NOT NULL REFERENCES sources(key) ON DELETE CASCADE,
                PRIMARY KEY(batch_id, source_key)
            );
            CREATE TABLE IF NOT EXISTS opinions (
                id TEXT PRIMARY KEY, source_key TEXT NOT NULL REFERENCES sources(key) ON DELETE CASCADE,
                module TEXT NOT NULL, type TEXT NOT NULL, sentiment TEXT NOT NULL,
                theme TEXT NOT NULL, evidence TEXT NOT NULL, origin TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY, source_key TEXT NOT NULL REFERENCES sources(key) ON DELETE CASCADE,
                created_at TEXT NOT NULL, event TEXT NOT NULL, snapshot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ledger (
                id TEXT PRIMARY KEY, source_key TEXT, created_at TEXT NOT NULL, month TEXT NOT NULL,
                status TEXT NOT NULL, amount_micros INTEGER NOT NULL DEFAULT 0,
                reserved_micros INTEGER NOT NULL, error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cache (fingerprint TEXT PRIMARY KEY, result TEXT NOT NULL);
            """)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
            for column, kind in [("analysis_text", "TEXT"), ("analysis_version", "INTEGER")]:
                if column not in columns:
                    conn.execute(f"ALTER TABLE sources ADD COLUMN {column} {kind}")
            additions = {
                "sources": {
                    "brand_relevance": "TEXT NOT NULL DEFAULT 'uncertain'",
                    "product_scope": "TEXT NOT NULL DEFAULT 'unknown'",
                    "content_types": "TEXT NOT NULL DEFAULT '[]'",
                    "review_reasons": "TEXT NOT NULL DEFAULT '[]'",
                    "filter_reason": "TEXT NOT NULL DEFAULT ''",
                    "analysis_schema": "TEXT NOT NULL DEFAULT 'legacy'",
                    "routing_schema": "TEXT NOT NULL DEFAULT 'legacy'",
                },
                "opinions": {
                    "submodule": "TEXT",
                    "aspect_category": "TEXT",
                    "target_text": "TEXT",
                    "opinion_text": "TEXT",
                    "feedback_type": "TEXT",
                    "sentiment_code": "TEXT",
                    "implicit_target": "INTEGER NOT NULL DEFAULT 0",
                    "implicit_opinion": "INTEGER NOT NULL DEFAULT 0",
                    "context_used": "INTEGER NOT NULL DEFAULT 0",
                    "needs_review": "INTEGER NOT NULL DEFAULT 0",
                    "schema_version": "TEXT NOT NULL DEFAULT 'legacy'",
                },
                "ledger": {
                    "outcome": "TEXT",
                    "finish_reason": "TEXT",
                    "json_valid": "INTEGER",
                    "duration_ms": "INTEGER",
                    "prompt_tokens": "INTEGER",
                    "completion_tokens": "INTEGER",
                    "retry_index": "INTEGER NOT NULL DEFAULT 0",
                    "model": "TEXT",
                    "rule_version": "TEXT",
                },
            }
            for table, fields in additions.items():
                existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                for column, declaration in fields.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
