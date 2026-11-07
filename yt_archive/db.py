from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .common import epoch, private_dir


class Database:
    def __init__(self, directory: Path):
        private_dir(directory)
        self.connection = sqlite3.connect(directory / "archive.sqlite3")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY, url TEXT NOT NULL, source TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '', members_only INTEGER NOT NULL DEFAULT 0,
                youtube_allowed INTEGER NOT NULL DEFAULT 1, discovered_at REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', local_path TEXT, metadata TEXT,
                file_size INTEGER, sha256 TEXT, drive_target TEXT, drive_root TEXT,
                drive_verified_at REAL, youtube_session TEXT, youtube_id TEXT,
                youtube_verified_at REAL, youtube_channel TEXT, completed_at REAL,
                youtube_thumbnail_sha256 TEXT, youtube_thumbnail_set_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                error TEXT, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scans (source TEXT PRIMARY KEY, full_scan_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, video_id TEXT NOT NULL, at REAL NOT NULL,
                state TEXT NOT NULL, detail TEXT
            );
            CREATE TABLE IF NOT EXISTS verification_checks (
                id INTEGER PRIMARY KEY, video_id TEXT NOT NULL, stage TEXT NOT NULL,
                at REAL NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL, error TEXT
            );
            CREATE INDEX IF NOT EXISTS checks_video_stage ON verification_checks(video_id,stage,id);
            CREATE INDEX IF NOT EXISTS events_video_state ON events(video_id,state,at);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
                status TEXT NOT NULL DEFAULT 'running', summary TEXT NOT NULL DEFAULT '{}'
            );
        """)
        # Existing archives acquire the thumbnail checkpoint without losing history.
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(videos)")}
        for name, kind in (("youtube_thumbnail_sha256", "TEXT"), ("youtube_thumbnail_set_at", "REAL")):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE videos ADD COLUMN {name} {kind}")
        self.connection.commit()
        (directory / "archive.sqlite3").chmod(0o600)

    def close(self):
        self.connection.close()

    def record_check(self, video_id, stage, evidence=None, *, status="verified", error=None):
        if stage not in {"download", "drive", "youtube", "cleanup"} or status not in {"verified", "failed", "waiting"}:
            raise ValueError("Unknown verification stage/status")
        with self.connection:
            self.connection.execute(
                "INSERT INTO verification_checks(video_id,stage,at,status,evidence,error) VALUES (?,?,?,?,?,?)",
                (video_id, stage, epoch(), status, json.dumps(evidence or {}, ensure_ascii=False), error))

    def start_run(self):
        with self.connection:
            return self.connection.execute("INSERT INTO runs(started_at) VALUES (?)", (epoch(),)).lastrowid

    def finish_run(self, run_id, status, summary):
        with self.connection:
            self.connection.execute("UPDATE runs SET finished_at=?,status=?,summary=? WHERE id=?",
                                    (epoch(), status, json.dumps(summary, ensure_ascii=False), run_id))

    def get(self, video_id):
        row = self.connection.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
        return dict(row) if row else None

    def add(self, video_id, url, source="manual", title="", members_only=False, youtube_allowed=True, discovered_at=None):
        now = epoch()
        with self.connection:
            self.connection.execute("""INSERT OR IGNORE INTO videos
                (video_id,url,source,title,members_only,youtube_allowed,discovered_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (video_id, url, source, title, int(members_only), int(youtube_allowed),
                 now if discovered_at is None else discovered_at, now))
            self.connection.execute("UPDATE videos SET members_only=?,youtube_allowed=? WHERE video_id=? AND source=?",
                                    (int(members_only), int(youtube_allowed), video_id, source))

    def update(self, video_id, **values):
        # Keys only originate in application code; never accept CLI field names.
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(videos)")}
        if not values.keys() <= columns - {"video_id"}:
            raise ValueError("Unknown database field")
        values["updated_at"] = epoch()
        with self.connection:
            self.connection.execute("UPDATE videos SET " + ",".join(f"{key}=?" for key in values) +
                                    " WHERE video_id=?", (*values.values(), video_id))
            if "state" in values:
                self.connection.execute("INSERT INTO events(video_id,at,state,detail) VALUES (?,?,?,?)",
                                        (video_id, epoch(), values["state"], values.get("error")))

    def jobs(self, config):
        rows = self.connection.execute("""SELECT * FROM videos WHERE state != 'skipped'
            AND retry_at <= ? ORDER BY discovered_at DESC, rowid ASC""", (epoch(),))
        jobs = []
        for row in rows:
            job = dict(row)
            if not config.job_enabled(job):
                continue
            if job["state"] != "complete" or not job["completed_at"] or config.wants_youtube(job) and not job["youtube_verified_at"]:
                jobs.append(job)
            if len(jobs) >= config.max_jobs:
                break
        return jobs

    def full_scan_due(self, source, hours):
        row = self.connection.execute("SELECT full_scan_at FROM scans WHERE source=?", (source,)).fetchone()
        return row is None or epoch() - row[0] >= hours * 3600

    def scanned(self, source):
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO scans VALUES (?,?)", (source, epoch()))

    def public_status(self):
        # Session URLs and local credentials are intentionally excluded.
        fields = ("video_id", "title", "state", "drive_target", "drive_verified_at", "youtube_id",
                  "youtube_verified_at", "youtube_thumbnail_set_at", "completed_at", "attempts", "retry_at", "error")
        rows = [dict(row) for row in self.connection.execute(
            "SELECT " + ",".join(fields) + " FROM videos ORDER BY discovered_at DESC")]
        for row in rows:
            row["youtube_url"] = f"https://www.youtube.com/watch?v={row['youtube_id']}" if row["youtube_id"] else None
        return rows

    def metadata(self, video_id):
        return json.loads(self.get(video_id)["metadata"] or "{}")
