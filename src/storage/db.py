# src/storage/db.py
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

import aiosqlite

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._run_migrations()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _run_migrations(self):
        async with self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ) as cursor:
            table_exists = await cursor.fetchone()
        current_version = 0
        if table_exists:
            async with self._conn.execute(
                "SELECT MAX(version) as v FROM schema_version"
            ) as cursor:
                row = await cursor.fetchone()
                current_version = row["v"] if row and row["v"] else 0

        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        for mf in migration_files:
            version = int(mf.stem.split("_")[0])
            if version > current_version:
                logger.info(f"Applying migration {mf.name}")
                sql = mf.read_text(encoding="utf-8")
                for statement in sql.split(";"):
                    stmt = statement.strip()
                    if stmt:
                        await self._conn.execute(stmt)
                # After executing migration statements, handle special cases
                if version == 2:
                    # FTS5 triggers can't go in SQL file (semicolons inside BEGIN..END break split)
                    await self._conn.executescript("""
                        CREATE TRIGGER IF NOT EXISTS raw_items_ai AFTER INSERT ON raw_items BEGIN
                            INSERT INTO items_fts(rowid, title, content, author)
                            VALUES (new.id, new.title, new.content, new.author);
                        END;
                        CREATE TRIGGER IF NOT EXISTS raw_items_ad AFTER DELETE ON raw_items BEGIN
                            INSERT INTO items_fts(items_fts, rowid, title, content, author)
                            VALUES ('delete', old.id, old.title, old.content, old.author);
                        END;
                    """)
                await self._conn.execute(
                    "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                    (version, mf.stem),
                )
                await self._conn.commit()

    async def get_tables(self) -> list[str]:
        async with self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["name"] for row in rows]

    async def get_schema_version(self) -> int:
        async with self._conn.execute("SELECT MAX(version) as v FROM schema_version") as cursor:
            row = await cursor.fetchone()
        return row["v"] if row and row["v"] else 0

    async def get_journal_mode(self) -> str:
        async with self._conn.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
        return row[0]

    async def insert_raw_item(self, item: dict) -> int | None:
        """Insert a raw item. Returns id or None if URL already exists."""
        try:
            async with self._conn.execute(
                """INSERT INTO raw_items
                   (source, title, url, author, published_at, content, metadata, batch_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item["source"], item["title"], item["url"],
                    item.get("author"), item["published_at"],
                    item.get("content", ""), item.get("metadata", "{}"),
                    item["batch_id"],
                ),
            ) as cursor:
                await self._conn.commit()
                return cursor.lastrowid
        except aiosqlite.IntegrityError:
            return None

    async def get_raw_item_by_url(self, url: str) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM raw_items WHERE url = ?", (url,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_recent_raw_items(self, since: datetime) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM raw_items WHERE collected_at >= ? ORDER BY published_at DESC",
            (since.isoformat(),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_unprocessed_raw_items(self, batch_id: str | None = None) -> list[dict]:
        if batch_id:
            query = """SELECT r.* FROM raw_items r
                       LEFT JOIN processed_items p ON r.id = p.raw_item_id
                       WHERE r.batch_id = ? AND p.id IS NULL"""
            params = (batch_id,)
        else:
            query = """SELECT r.* FROM raw_items r
                       LEFT JOIN processed_items p ON r.id = p.raw_item_id
                       WHERE p.id IS NULL"""
            params = ()
        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def insert_processed_item(self, item: dict) -> int | None:
        try:
            async with self._conn.execute(
                """INSERT INTO processed_items
                   (raw_item_id, passed_filter, filter_reason, score_total,
                    score_breakdown, summary, practice_points, tags, related_sources)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item["raw_item_id"], item["passed_filter"],
                    item.get("filter_reason"), item.get("score_total"),
                    item.get("score_breakdown"), item.get("summary"),
                    item.get("practice_points"), item.get("tags"),
                    item.get("related_sources"),
                ),
            ) as cursor:
                await self._conn.commit()
                return cursor.lastrowid
        except aiosqlite.IntegrityError:
            return None

    async def get_undigested_items(self) -> list[dict]:
        async with self._conn.execute(
            """SELECT p.*, r.source, r.title, r.url, r.author, r.published_at,
                      r.content, r.metadata
               FROM processed_items p
               JOIN raw_items r ON p.raw_item_id = r.id
               WHERE p.passed_filter = 1 AND p.digested = 0
               ORDER BY p.score_total DESC""",
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def mark_items_digested(self, item_ids: list[int]):
        if not item_ids:
            return
        placeholders = ",".join("?" * len(item_ids))
        await self._conn.execute(
            f"UPDATE processed_items SET digested = 1 WHERE id IN ({placeholders})",
            item_ids,
        )
        await self._conn.commit()

    async def insert_notification(self, channel: str, digest_date: str,
                                  item_count: int, status: str, error: str = None):
        try:
            await self._conn.execute(
                """INSERT INTO notifications (channel, digest_date, item_count, status, error_message)
                   VALUES (?, ?, ?, ?, ?)""",
                (channel, digest_date, item_count, status, error),
            )
            await self._conn.commit()
        except Exception as e:
            logger.error(f"Failed to insert notification: {e}")

    async def insert_collection_run(self, run_id: str, status: str = "running"):
        try:
            await self._conn.execute(
                "INSERT INTO collection_runs (id, started_at, status) VALUES (?, ?, ?)",
                (run_id, datetime.now(timezone.utc).isoformat(), status),
            )
            await self._conn.commit()
        except Exception as e:
            logger.error(f"Failed to insert collection run: {e}")

    _ALLOWED_RUN_COLUMNS = {"status", "finished_at", "total_collected", "total_passed", "error_message", "lookback_hours", "sources_collected"}

    async def update_collection_run(self, run_id: str, **kwargs):
        invalid = set(kwargs) - self._ALLOWED_RUN_COLUMNS
        if invalid:
            raise ValueError(f"Invalid column(s) for collection_runs: {invalid}")
        try:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            vals = list(kwargs.values()) + [run_id]
            await self._conn.execute(
                f"UPDATE collection_runs SET {sets} WHERE id = ?", vals
            )
            await self._conn.commit()
        except Exception as e:
            logger.error(f"Failed to update collection run: {e}")

    async def get_extracted_content(self, url: str) -> str | None:
        async with self._conn.execute(
            "SELECT content FROM extracted_content WHERE url = ?", (url,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["content"] if row else None

    async def save_extracted_content(self, url: str, content: str | None):
        await self._conn.execute(
            "INSERT OR REPLACE INTO extracted_content (url, content) VALUES (?, ?)",
            (url, content),
        )
        await self._conn.commit()

    async def insert_score(self, raw_item_id: int, passed: bool, category: str,
                           total_score: float, breakdown: dict, model: str = "") -> int | None:
        try:
            import json
            cursor = await self._conn.execute(
                """INSERT OR REPLACE INTO scores
                   (raw_item_id, passed_filter, category, total_score, score_breakdown, model_used)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (raw_item_id, passed, category, total_score, json.dumps(breakdown), model),
            )
            await self._conn.commit()
            return cursor.lastrowid
        except Exception as e:
            logger.error("insert_score failed: %s", e)
            return None

    async def search(self, query: str, category: str = None, min_score: float = None,
                     tier: int = None, limit: int = 50) -> list[dict]:
        from src.prompts.filter import TIER_MAP
        sql = """SELECT r.*, s.total_score, s.score_breakdown, s.category, s.filter_reason
                 FROM items_fts f
                 JOIN raw_items r ON f.rowid = r.id
                 LEFT JOIN scores s ON r.id = s.raw_item_id
                 WHERE items_fts MATCH ?"""
        params = [query]

        if category:
            sql += " AND s.category = ?"
            params.append(category)
        if min_score is not None:
            sql += " AND s.total_score >= ?"
            params.append(min_score)
        if tier is not None:
            tier_cats = [c for c, t in TIER_MAP.items() if t == tier]
            if tier_cats:
                placeholders = ",".join("?" * len(tier_cats))
                sql += f" AND s.category IN ({placeholders})"
                params.extend(tier_cats)

        sql += " ORDER BY s.total_score DESC LIMIT ?"
        params.append(limit)

        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_top(self, n: int = 20, tier: int = None) -> list[dict]:
        from src.prompts.filter import TIER_MAP
        sql = """SELECT r.*, s.total_score, s.score_breakdown, s.category
                 FROM scores s JOIN raw_items r ON s.raw_item_id = r.id
                 WHERE s.passed_filter = 1"""
        params = []
        if tier is not None:
            tier_cats = [c for c, t in TIER_MAP.items() if t == tier]
            if tier_cats:
                placeholders = ",".join("?" * len(tier_cats))
                sql += f" AND s.category IN ({placeholders})"
                params.extend(tier_cats)
        sql += " ORDER BY s.total_score DESC LIMIT ?"
        params.append(n)
        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_raw_item(self, item_id: int) -> dict | None:
        cursor = await self._conn.execute("SELECT * FROM raw_items WHERE id = ?", (item_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_summary(self, raw_item_id: int) -> str | None:
        cursor = await self._conn.execute(
            "SELECT summary_text FROM summaries WHERE raw_item_id = ? ORDER BY created_at DESC LIMIT 1",
            (raw_item_id,),
        )
        row = await cursor.fetchone()
        return row["summary_text"] if row else None

    async def insert_summary(self, raw_item_id: int, text: str,
                             subtitle_source: str = "", model: str = "") -> int | None:
        try:
            cursor = await self._conn.execute(
                "INSERT INTO summaries (raw_item_id, summary_text, subtitle_source, model_used) VALUES (?, ?, ?, ?)",
                (raw_item_id, text, subtitle_source, model),
            )
            await self._conn.commit()
            return cursor.lastrowid
        except Exception as e:
            logger.error("insert_summary failed: %s", e)
            return None

    async def get_stats(self) -> dict:
        stats = {}
        # Total items
        cursor = await self._conn.execute("SELECT COUNT(*) as cnt FROM raw_items")
        stats["total_raw"] = (await cursor.fetchone())["cnt"]
        # By source
        cursor = await self._conn.execute("SELECT source, COUNT(*) as cnt FROM raw_items GROUP BY source")
        stats["by_source"] = {row["source"]: row["cnt"] for row in await cursor.fetchall()}
        # Scored items
        cursor = await self._conn.execute("SELECT COUNT(*) as cnt FROM scores WHERE passed_filter = 1")
        stats["total_passed"] = (await cursor.fetchone())["cnt"]
        # By category
        cursor = await self._conn.execute(
            "SELECT category, COUNT(*) as cnt, AVG(total_score) as avg_score "
            "FROM scores WHERE passed_filter = 1 GROUP BY category"
        )
        stats["by_category"] = {row["category"]: {"count": row["cnt"], "avg_score": round(row["avg_score"], 2)}
                                 for row in await cursor.fetchall()}
        # Summaries count
        cursor = await self._conn.execute("SELECT COUNT(*) as cnt FROM summaries")
        stats["total_summaries"] = (await cursor.fetchone())["cnt"]
        return stats

    async def cleanup_expired(self, raw_days: int = 30, processed_days: int = 90):
        now = datetime.now(timezone.utc)
        raw_cutoff = (now - timedelta(days=raw_days)).isoformat()
        processed_cutoff = (now - timedelta(days=processed_days)).isoformat()

        await self._conn.execute(
            "DELETE FROM raw_items WHERE collected_at < ? AND id NOT IN "
            "(SELECT raw_item_id FROM processed_items WHERE passed_filter = 1)",
            (raw_cutoff,),
        )
        # Delete rejected items after raw_days
        await self._conn.execute(
            "DELETE FROM processed_items WHERE processed_at < ? AND passed_filter = 0",
            (raw_cutoff,),
        )
        # Delete all processed items (including passed) after processed_days
        await self._conn.execute(
            "DELETE FROM processed_items WHERE processed_at < ?",
            (processed_cutoff,),
        )
        await self._conn.execute(
            "DELETE FROM extracted_content WHERE extracted_at < ?",
            (raw_cutoff,),
        )
        await self._conn.commit()

    async def get_all_raw_items(self, since: datetime = None) -> list[dict]:
        """Get all raw items, optionally filtered by time."""
        if since:
            sql = "SELECT * FROM raw_items WHERE collected_at >= ? ORDER BY published_at DESC"
            cursor = await self._conn.execute(sql, (since.isoformat(),))
        else:
            sql = "SELECT * FROM raw_items ORDER BY published_at DESC"
            cursor = await self._conn.execute(sql)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def clear_processed_items(self, raw_item_ids: list[int] = None):
        """Delete processed items + scores for rescoring. If ids=None, clear all."""
        if raw_item_ids:
            placeholders = ",".join("?" * len(raw_item_ids))
            await self._conn.execute(
                f"DELETE FROM processed_items WHERE raw_item_id IN ({placeholders})",
                raw_item_ids,
            )
            await self._conn.execute(
                f"DELETE FROM scores WHERE raw_item_id IN ({placeholders})",
                raw_item_ids,
            )
        else:
            await self._conn.execute("DELETE FROM processed_items")
            await self._conn.execute("DELETE FROM scores")
        await self._conn.commit()

    async def get_weekly_raw_counts(self, since: datetime) -> dict[str, int]:
        """Get raw item counts per source since a given date (before filtering)."""
        async with self._conn.execute(
            "SELECT source, COUNT(*) as cnt FROM raw_items WHERE collected_at >= ? GROUP BY source",
            (since.isoformat(),),
        ) as cursor:
            rows = await cursor.fetchall()
        return {row["source"]: row["cnt"] for row in rows}

    async def get_weekly_items(self, since: datetime) -> list[dict]:
        """Get all processed+passed items since a given date, with raw item data joined."""
        async with self._conn.execute(
            """SELECT p.*, r.source, r.title, r.url, r.author, r.published_at,
                      r.content, r.metadata
               FROM processed_items p
               JOIN raw_items r ON p.raw_item_id = r.id
               WHERE p.passed_filter = 1
                 AND r.collected_at >= ?
               ORDER BY r.source, p.score_total DESC""",
            (since.isoformat(),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]
