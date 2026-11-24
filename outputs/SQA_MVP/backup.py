"""Create and verify local, versioned backups without changing live data."""

import hashlib
import json
import re
import sqlite3
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from store import LOCAL_TZ, now

LOCK = threading.Lock()
DAILY_NAME = re.compile(r"sqa-daily-(\d{4}-\d{2}-\d{2})\.zip")
LOCAL_SETTINGS = {"workdays.json", "stage-reviews.json", "crash-site-state.json"}


def referenced_files(database):
    with closing(sqlite3.connect(database)) as db:
        rows = db.execute("SELECT manifest FROM batches WHERE payload IS NOT NULL")
        files = {item["sha256"] for (manifest,) in rows for item in json.loads(manifest).values()}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='governance_blobs'").fetchone():
            files.update(row[0] for row in db.execute("SELECT sha256 FROM governance_blobs"))
        return files


def backup(store, reason):
    with LOCK:
        directory = store.root / "backups"
        directory.mkdir(exist_ok=True)
        stamp = datetime.now(LOCAL_TZ)
        suffix = stamp.date().isoformat() if reason == "daily" else stamp.strftime("%Y%m%dT%H%M%S%f")
        target = directory / f"sqa-{reason}-{suffix}.zip"
        if reason == "daily" and target.exists():
            return target
        with tempfile.TemporaryDirectory(prefix="backing-up-", dir=directory) as scratch:
            scratch = Path(scratch)
            snapshot = scratch / "pilot.sqlite3"
            source_db = sqlite3.connect(store.db_file, timeout=10)
            output_db = sqlite3.connect(snapshot)
            try:
                source_db.backup(output_db)
            finally:
                output_db.close()
                source_db.close()
            hashes = {"pilot.sqlite3": hashlib.sha256(snapshot.read_bytes()).hexdigest()}
            work_zip = scratch / "backup.zip"
            with ZipFile(work_zip, "w", compression=ZIP_DEFLATED) as archive:
                archive.write(snapshot, "pilot.sqlite3")
                for name in sorted(LOCAL_SETTINGS):
                    setting = store.root / name
                    if setting.is_file():
                        raw = setting.read_bytes()
                        archive.writestr(name, raw)
                        hashes[name] = hashlib.sha256(raw).hexdigest()
                for digest in sorted(referenced_files(snapshot)):
                    path = store.sources / digest
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != digest:
                        raise ValueError("来源文件校验失败")
                    key = f"sources/{digest}"
                    archive.writestr(key, raw)
                    hashes[key] = digest
                archive.writestr("backup-manifest.json", json.dumps({
                    "created": now(), "reason": reason, "sha256": hashes,
                }, ensure_ascii=False))
            work_zip.replace(target)
        if reason == "daily":
            limit = stamp.date() - timedelta(days=30)
            for previous in directory.iterdir():
                match = DAILY_NAME.fullmatch(previous.name)
                if match and match[1] <= limit.isoformat() and previous.is_file() and not previous.is_symlink():
                    previous.unlink()
        return target


def restore(archive_path, destination):
    """Verify all entries and restore into a new directory."""
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("恢复目录必须是不存在的新目录")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sqa-restore-", dir=destination.parent) as scratch:
        staged = Path(scratch) / "verified"
        (staged / "sources").mkdir(parents=True)
        with ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("backup-manifest.json"))
            hashes = manifest["sha256"]
            if "pilot.sqlite3" not in hashes or sorted(archive.namelist()) != sorted([*hashes, "backup-manifest.json"]):
                raise ValueError("备份清单不完整")
            for name, digest in hashes.items():
                if name not in LOCAL_SETTINGS | {"pilot.sqlite3"} and name != f"sources/{digest}":
                    raise ValueError("备份包含无效文件路径")
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("备份哈希格式无效")
                data = archive.read(name)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError("备份哈希校验失败")
                (staged / name).write_bytes(data)
        database = staged / "pilot.sqlite3"
        db = sqlite3.connect(database)
        try:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("数据库完整性检查失败")
        finally:
            db.close()
        if referenced_files(database) != {p.name for p in (staged / "sources").iterdir()}:
            raise ValueError("证据清单和数据库不一致")
        staged.rename(destination)
    return destination
