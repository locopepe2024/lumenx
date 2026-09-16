"""Owner-scoped source registration and durable, revision-checked analysis jobs."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import time
from uuid import uuid4

from fastapi import HTTPException

from ..identity import UserContext
from ..studio_access import studio_owner_dir
from .analysis import MAX_BYTES, analyze, extract_pair, fingerprint

LEASE_SECONDS = 600


class RecreationService:
    def __init__(self, user: UserContext):
        if not user.user_id or not user.owner_profile_id:
            raise ValueError("Authenticated owner required")
        self.user = user
        self.root = (Path(studio_owner_dir(user.owner_profile_id)) / "recreation").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def db(self):
        connection = sqlite3.connect("output/recreation.sqlite3", timeout=10)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS media_records (media_id TEXT PRIMARY KEY, owner TEXT NOT NULL, project_id TEXT, kind TEXT NOT NULL, display_name TEXT NOT NULL, storage_path TEXT NOT NULL, sha256 TEXT NOT NULL, metadata TEXT NOT NULL, created_at REAL NOT NULL)")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _get(self, db, project_id):
        row = db.execute("SELECT data FROM projects WHERE id=? AND owner=?",
                         (project_id, self.user.owner_profile_id)).fetchone()
        if not row:
            raise HTTPException(404, "Recreation project not found")
        return json.loads(row[0])

    def _save(self, db, record):
        record["revision"] += 1
        record["updated_at"] = time.time()
        db.execute("UPDATE projects SET data=? WHERE id=? AND owner=?",
                   (json.dumps(record), record["id"], self.user.owner_profile_id))

    def _path(self, stored):
        path = (Path("output") / stored).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Source is missing or outside the project owner")
        return path

    def register(self, stream, filename: str):
        suffix = Path(filename).suffix.lower()
        if suffix not in (".mp4", ".mov", ".webm", ".mkv"):
            raise HTTPException(422, "Supported sources: MP4, MOV, WebM, MKV")
        project_id = uuid4().hex
        folder = self.root / project_id
        folder.mkdir()
        source = folder / ("source" + suffix)
        try:
            size = 0
            with source.open("xb") as target:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise HTTPException(413, "Source exceeds 256 MiB")
                    target.write(chunk)
            digest = fingerprint(source)
            media_id = uuid4().hex
            record = {"id": project_id, "title": Path(filename).name[:200], "source_media_id": media_id,
                      "owner_user_id": self.user.user_id, "owner_profile_id": self.user.owner_profile_id,
                      "source_asset_id": uuid4().hex,
                      "source_url": source.relative_to(Path("output").resolve()).as_posix(),
                      "source_fingerprint": digest, "status": "registered", "revision": 0,
                      "created_at": time.time(), "updated_at": time.time(), "analysis": None,
                      "analysis_id": None, "timeline": None, "error": None, "attempt": 0}
            with self.db() as db:
                db.execute("INSERT INTO projects VALUES (?, ?, ?)",
                           (project_id, self.user.owner_profile_id, json.dumps(record)))
                db.execute("INSERT INTO media_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (media_id, self.user.owner_profile_id, project_id, "source_video", record["title"], record["source_url"], digest, json.dumps({"mime": "video"}), time.time()))
            return record
        except BaseException:
            shutil.rmtree(folder)
            raise

    def get(self, project_id):
        with self.db() as db:
            record = self._get(db, project_id)
            if record["status"] in ("queued", "analyzing") and time.time() - record["started_at"] > LEASE_SECONDS:
                record.update(status="failed", error="Analysis interrupted or timed out; retry available")
                self._save(db, record)
            return record

    def list(self):
        with self.db() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM projects WHERE owner=?", (self.user.owner_profile_id,))]
        return sorted([self.get(i) for i in ids], key=lambda p: p["created_at"], reverse=True)

    def search_media(self, *, query="", kind=None, project_id=None, limit=50, cursor=0):
        limit = max(1, min(int(limit), 100))
        query = query.strip().lower()
        with self.db() as db:
            rows = db.execute("SELECT media_id, project_id, kind, display_name, storage_path, sha256, metadata, created_at FROM media_records WHERE owner=? ORDER BY created_at DESC", (self.user.owner_profile_id,)).fetchall()
        items = []
        for row in rows:
            if kind and row[2] != kind or project_id and row[1] != project_id: continue
            if query and query not in row[3].lower(): continue
            items.append({"media_id": row[0], "project_id": row[1], "kind": row[2], "display_name": row[3], "storage_path": row[4], "sha256": row[5], "metadata": json.loads(row[6]), "created_at": row[7]})
        return {"items": items[cursor:cursor + limit], "next_cursor": cursor + limit if cursor + limit < len(items) else None}

    def start(self, project_id, revision):
        with self.db() as db:
            record = self._get(db, project_id)
            if record["revision"] != revision:
                raise HTTPException(409, "Project changed; refresh before analyzing")
            if record["status"] in ("queued", "analyzing") and time.time() - record["started_at"] <= LEASE_SECONDS:
                raise HTTPException(409, "Analysis already in progress")
            active = db.execute("SELECT COUNT(*) FROM projects WHERE json_extract(data, '$.status') IN ('queued','analyzing') AND json_extract(data, '$.started_at') > ?",
                                (time.time() - LEASE_SECONDS,)).fetchone()[0]
            if active >= 2:
                raise HTTPException(429, "Analysis capacity is busy; retry later")
            attempt_id = uuid4().hex
            record.update(status="queued", error=None, analysis_id=attempt_id,
                          started_at=time.time(), attempt=record["attempt"] + 1,
                          analysis=None, timeline=None)
            self._save(db, record)
        return record

    def evidence(self, project_id, analysis_id, cut):
        record = self.get(project_id)
        if record["analysis_id"] != analysis_id or not record["analysis"]:
            raise HTTPException(409, "Analysis changed; refresh before selecting a cut")
        return extract_pair(self._path(record["source_url"]),
                            self.root / project_id / analysis_id / uuid4().hex, record["analysis"], cut)

    def process(self, project_id, attempt_id):
        with self.db() as db:
            record = self._get(db, project_id)
            if record["analysis_id"] != attempt_id or record["status"] != "queued":
                return
            record["status"] = "analyzing"
            self._save(db, record)
        try:
            result = analyze(self._path(record["source_url"]), self.root / project_id / attempt_id,
                             record["source_fingerprint"])
            error = None
        except Exception as exc:
            result = None
            error = str(exc) if isinstance(exc, ValueError) else "Analysis failed; retry or register another source"
        with self.db() as db:
            current = self._get(db, project_id)
            if current["analysis_id"] != attempt_id or current["status"] != "analyzing":
                return
            current.update(analysis=result, error=error, status="failed" if error else "review")
            self._save(db, current)

    def confirm(self, project_id, revision, analysis_id, cuts):
        # Hashing is outside the write transaction; source files are immutable after registration.
        snapshot = self.get(project_id)
        if fingerprint(self._path(snapshot["source_url"])) != snapshot["source_fingerprint"]:
            raise HTTPException(409, "Source fingerprint changed; register the source again")
        with self.db() as db:
            record = self._get(db, project_id)
            if record["revision"] != revision or record["analysis_id"] != analysis_id:
                raise HTTPException(409, "Analysis changed; refresh before confirming")
            if record["status"] not in ("review", "confirmed") or not record["analysis"]:
                raise HTTPException(409, "Source analysis is not ready")
            analysis = record["analysis"]
            valid = set(analysis["frame_pts"][1:])
            if len(cuts) > 120 or cuts != sorted(set(cuts)) or any(type(p) is not int or p not in valid for p in cuts):
                raise HTTPException(422, "Cuts must be ordered unique source-frame PTS, excluding the start")
            boundaries = [analysis["start_pts"], *cuts, analysis["end_pts"]]
            detected = {c["pts"] for c in analysis["candidates"]}
            record["timeline"] = {"analysis_id": analysis_id, "source_fingerprint": record["source_fingerprint"],
                                  "time_base": analysis["time_base"], "confirmed_at": time.time(),
                                  "cuts": [{"pts": p, "source": "detected" if p in detected else "manual",
                                            "confirmed": True} for p in cuts],
                                  "shots": [{"id": uuid4().hex, "start_pts": a, "end_pts": b}
                                            for a, b in zip(boundaries, boundaries[1:])]}
            record["status"] = "confirmed"
            self._save(db, record)
        return record
