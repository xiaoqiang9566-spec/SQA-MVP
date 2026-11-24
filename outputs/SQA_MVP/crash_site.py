"""Read-only publication checks for the explicitly configured office crash dashboard."""

import hashlib
import json
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from store import now, need, Problem

SITE = "http://10.134.23.50:8765/dashboard/"
OUTPUTS = "http://10.134.23.50:8765/outputs/"
CONTRACT = "same_day_android_ios_matched_crash_device_days_v2"
HASH = re.compile(r"[0-9a-f]{64}", re.I)
LIMIT = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("源站发生重定向，需重新核对接入地址")


def filename(value, suffix):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][\w.-]*", value) or ".." in value or not value.endswith(suffix):
        raise ValueError("源站清单文件名无效")
    return value


def read_file(name, limit=LIMIT):
    # Never forward session cookies, proxy credentials, or follow arbitrary URLs.
    filename(name, ".csv" if name.endswith(".csv") else ".json")
    request = Request(OUTPUTS + name, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15 if name.endswith(".csv") else 4) as response:
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("源站发布清单超过大小限制")
        return raw


def read_publication(reader=read_file, resources=None):
    pointer_raw = reader("dashboard_latest.json")
    pointer = json.loads(pointer_raw)
    if pointer.get("schema_version") != 1 or pointer.get("candidate") is not False:
        raise ValueError("源站指针不是正式发布快照")
    if not isinstance(pointer.get("snapshot_id"), str) or not re.fullmatch(r"[\w.-]{1,128}", pointer["snapshot_id"]):
        raise ValueError("源站快照标识无效")
    stamp = datetime.fromisoformat(pointer["published_at_utc"].replace("Z", "+00:00"))
    if stamp.utcoffset() != timedelta(0):
        raise ValueError("源站发布时间必须为 UTC")
    manifests = []
    allowed = {}
    documents = {"dashboard_latest.json": pointer_raw}
    for kind in ("trend", "signature"):
        name = filename(pointer[kind + "_manifest"], ".json")
        digest = pointer[kind + "_sha256"]
        if not isinstance(digest, str) or not HASH.fullmatch(digest):
            raise ValueError("源站清单哈希无效")
        raw = reader(name)
        if hashlib.sha256(raw).hexdigest() != digest.lower():
            raise ValueError("源站清单哈希校验失败")
        manifest = json.loads(raw)
        documents[name] = raw
        allowed[name] = digest.lower()
        if (manifest.get("schema_version") != 2 or manifest.get("snapshot_id") != pointer["snapshot_id"]
                or manifest.get("metric_contract") != CONTRACT
                or sorted(manifest.get("covered_platforms", [])) != ["android", "ios"]
                or manifest.get("validation", {}).get("status") != "VERIFIED"):
            raise ValueError("源站清单代次、口径或验证状态不符合接入约定")
        start, end = date.fromisoformat(manifest["start_date"]), date.fromisoformat(manifest["end_date"])
        if not 0 <= (end-start).days <= 3653:
            raise ValueError("源站日期范围无效")
        days = [(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
        if manifest.get("queried_days") != days:
            raise ValueError("源站查询日期不完整")
        files, metadata = manifest["files"], manifest["file_metadata"]
        if not files or len(set(files)) != len(files) or len(metadata) != len(files):
            raise ValueError("源站分片清单不完整")
        for file in files:
            filename(file, ".csv")
            matches = [record for record in metadata if record.get("file") == file]
            if (len(matches) != 1 or not HASH.fullmatch(matches[0].get("sha256", ""))
                    or type(matches[0].get("row_count")) is not int or matches[0]["row_count"] < 0):
                raise ValueError("源站分片元数据无效")
            allowed[file] = matches[0]["sha256"].lower()
        manifests.append(manifest)
    trend, signature = manifests
    for field in ("start_date", "end_date", "queried_days", "firmware_scope"):
        if trend[field] != signature[field]:
            raise ValueError("趋势与类型清单范围不一致")
    if (signature.get("parent_snapshot_file") != pointer["trend_manifest"]
            or signature.get("parent_snapshot_sha256") != pointer["trend_sha256"]):
        raise ValueError("崩溃类型父快照不一致")
    if resources is not None:
        resources.update(allowed=allowed, documents=documents)
    return {"snapshot_id": pointer["snapshot_id"], "revision": hashlib.sha256(pointer_raw).hexdigest(),
            "published_at_utc": pointer["published_at_utc"], "start_date": trend["start_date"],
            "end_date": trend["end_date"], "covered_platforms": trend["covered_platforms"],
            "trend_manifest": pointer["trend_manifest"], "signature_manifest": pointer["signature_manifest"],
            "trend_sha256": pointer["trend_sha256"], "signature_sha256": pointer["signature_sha256"],
            "metric_contract": CONTRACT}


class CrashSite:
    def __init__(self, config_path, root):
        config = json.loads(Path(config_path).read_text(encoding="utf-8-sig")) if config_path else {}
        self.enabled = config.get("crash_site") == SITE
        self.path = Path(root) / "crash-site-state.json"
        self.lock = Lock()
        self.resources = {"allowed": {}, "documents": {}}
        self.cache = Path(root) / "crash-cache"
        self.expires = 0
        self.state = {"checked": "", "error": "", "publication": None}
        if self.enabled and self.path.is_file():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if saved.get("source_url") == SITE:
                    self.state = {"checked": saved.get("checked", ""), "error": "尚待本次连接检查", "publication": saved.get("publication")}
            except (OSError, ValueError):
                pass

    def refresh(self, force=False):
        with self.lock:
            if not self.enabled or (not force and monotonic() < self.expires):
                return self.result()
            self.expires = monotonic() + 300
            self.state["checked"] = now()
            try:
                resources = {}
                publication = read_publication(resources=resources)
                saved = {"source_url": SITE, "checked": self.state["checked"], "publication": publication}
                staging = self.path.with_suffix(".tmp")
                staging.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
                staging.replace(self.path)
                self.state.update(publication=publication, error="")
                self.resources["allowed"].update(resources["allowed"])
                self.resources["documents"].update(resources["documents"])
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.state["error"] = "源站连接或发布清单校验失败；已保留上次通过校验的发布信息。"
            return self.result()

    def result(self):
        return {"enabled": self.enabled, "source_url": SITE, "interval_seconds": 300, **self.state}

    def download(self, name):
        need(self.enabled, "崩溃源站尚未配置", 404)
        self.refresh()
        with self.lock:
            document = self.resources["documents"].get(name)
            digest = self.resources["allowed"].get(name)
        if document is not None:
            return document
        need(digest, "文件不在已核验发布清单中", 404)
        self.cache.mkdir(exist_ok=True)
        cached = self.cache / digest
        try:
            if cached.is_file():
                raw = cached.read_bytes()
                if hashlib.sha256(raw).hexdigest() == digest:
                    return raw
            raw = read_file(name, 32 * 1024 * 1024)
            need(hashlib.sha256(raw).hexdigest() == digest, "源站数据分片哈希不一致", 502)
            with tempfile.NamedTemporaryFile(dir=self.cache, delete=False) as output:
                temporary = Path(output.name)
                output.write(raw)
            try:
                temporary.replace(cached)
            finally:
                temporary.unlink(missing_ok=True)
            return raw
        except (OSError, ValueError) as exc:
            raise Problem("源站数据读取失败，请稍后重试", 502) from exc
