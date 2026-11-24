"""Local polling of approved producer exports and optional Targetprocess reads."""

import csv
import hashlib
import io
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from imports import IMPORT_FILES, RID_LABEL, VERSION, pr_sources, read_csv
from store import LOCAL_TZ, MAX_FILE, Problem, cutoff_date, now

BUG_COLUMNS = ("ID", "Name", "Severity", "Entity State", "Teams", "Release", "Feature", "Tags", "Create Date")
BUG_DATE = re.compile(r"NG3 All Bug list - (\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})(\d{2})(?:\.\d+)?\.csv")
TP_ENDPOINT = "https://suunto.tpondemand.com/api/v2/Bug"
TP_FIELDS = ("id", "name", "severity", "state", "team", "release", "feature", "tags", "created")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def read_stable(path):
    before = path.stat()
    if before.st_size == 0 or before.st_size > MAX_FILE:
        raise Problem("来源文件为空或超出大小限制")
    content = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(content) != after.st_size:
        raise Problem("来源正在写入，稍后重试")
    return path.name, content


def latest_file(block, dated=False):
    directory = Path(block["directory"])
    paths = (path for path in directory.glob(block["glob"]) if path.is_file())
    if dated:
        paths = ((match.groups(), path) for path in paths if (match := BUG_DATE.fullmatch(path.name)))
        found = max(paths, key=lambda item: (item[0], item[1].name), default=None)
        if found:
            (day, hour, minute, second), path = found
            return path, f"{day}T{hour}:{minute}:{second}"
    else:
        found = max(paths, key=lambda path: (path.stat().st_mtime_ns, path.name), default=None)
        if found:
            return found
    raise Problem("暂未找到可同步的来源文件")


def tp_json(query, secret):
    url = TP_ENDPOINT + "?" + urlencode(query)
    request = Request(url, headers={"Accept": "application/json", "apptio-opentoken": secret}, method="GET")
    try:
        with build_opener(NoRedirect()).open(request, timeout=12) as response:
            raw = response.read(MAX_FILE + 1)
            if len(raw) > MAX_FILE:
                raise Problem("TP 单页响应超出大小限制")
            return json.loads(raw)
    except HTTPError as exc:
        raise Problem(f"TP API 返回 HTTP {exc.code}") from None
    except (URLError, TimeoutError):
        raise Problem("TP API 暂时不可达") from None
    except (ValueError, UnicodeError):
        raise Problem("TP API 响应格式无效") from None


def tp_snapshot(config, fetch=tp_json):
    token = os.environ.get("SQA_TP_OPENTOKEN")
    if not token:
        raise Problem("TP API 待读取本机授权凭证 SQA_TP_OPENTOKEN")
    fields = config.get("fields")
    where = str(config.get("where", "")).strip()
    if not where or not isinstance(fields, dict) or set(fields) != set(TP_FIELDS):
        raise Problem("TP API 待提供已验证的看板范围和字段映射")
    if any(not isinstance(value, str) or not value.strip() for value in fields.values()):
        raise Problem("TP API 字段映射无效")
    query = {"where": where, "select": "{" + ",".join(f"{key}:{fields[key]}" for key in TP_FIELDS) + "}",
             "orderBy": "id", "take": "250"}
    collected_at = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    rows = []
    seen = set()
    for page in range(40):
        value = fetch({**query, "skip": str(page * 250)}, token)
        items = value.get("items") if isinstance(value, dict) else None
        if not isinstance(items, list) or len(items) > 250:
            raise Problem("TP API 分页响应不符合契约")
        for item in items:
            if not isinstance(item, dict):
                raise Problem("TP API 缺陷记录异常")
            required = ("id", "name", "severity", "state", "team", "release")
            if any(item.get(key) is None or not isinstance(item[key], (str, int)) for key in required):
                raise Problem("TP 核心字段映射缺失或出现多值")
            identity = str(item["id"])
            if not identity.isdecimal() or identity in seen:
                raise Problem("TP 分页出现重复或无效 Bug ID")
            seen.add(identity)
            rows.append([item.get(key, "") for key in TP_FIELDS])
        if len(items) < 250:
            break
    else:
        raise Problem("TP 查询超过试点范围，缩小已授权查询后重试")
    if not rows:
        raise Problem("TP 未返回缺陷，保留当前看板")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(BUG_COLUMNS)
    writer.writerows(rows)
    return {"snapshot": ("targetprocess-bugs.csv", stream.getvalue().encode("utf-8"))}, collected_at


def active_payload(store, kind, version):
    with store.connection() as db:
        return store.active(db, kind, version)


def raw_rid_ids(store, user, batch, payload):
    if "rid_ids" in payload:
        return set(payload["rid_ids"])
    _, raw = store.source_download(user, batch, "snapshot")
    rows = read_csv(raw, ("ID", "Release"))
    return {row["ID"] for row in rows if row["Release"] == RID_LABEL}


def hold_reason(store, user, kind, version, batch, history=False):
    with store.connection() as db:
        result = db.execute("SELECT payload FROM batches WHERE id=?", (batch,)).fetchone()
    candidate = json.loads(result["payload"])
    old_batch, old = active_payload(store, kind, version)
    if kind == "pr":
        if history:
            if any(len(item["rules"]) != 8 or (not item["pr_id"] and
                   any(rule["result"] != "无法判定" for rule in item["rules"])) for item in candidate["entries"]):
                return old_batch, "历史审计规则缺失或未匹配条目的结论需要复核"
        elif any(not item["pr_id"] or not item["rules"] for item in candidate["entries"]):
            return old_batch, "存在未唯一匹配或缺少规则证据的 PR，等待复核"
        if old:
            def keys(payload):
                return {(item["key"], item["pr_id"], rule["rule_id"])
                        for item in payload["entries"] for rule in item["rules"]}
            if keys(old) - keys(candidate):
                return old_batch, "PR 匹配或规则覆盖发生变化，等待复核"
    if kind == "metrics" and old:
        def configuration(payload):
            return {row["id"] for row in payload["items"]}
        def snapshots(payload):
            return {(snap["label"], row["id"]) for snap in payload["snapshots"] for row in snap["rows"]}
        if configuration(old) - configuration(candidate) or snapshots(old) - snapshots(candidate):
            return old_batch, "指标配置或历史快照覆盖减少，等待复核"
    if kind == "bugs" and old:
        if candidate["cutoff"] < old["cutoff"]:
            return old_batch, "快照早于当前数据，保留当前看板"
        previous = raw_rid_ids(store, user, old_batch, old)
        current = set(candidate["rid_ids"])
        if previous - current:
            return old_batch, f"RID 源记录 {len(previous)} → {len(current)}，有 {len(previous-current)} 个原 ID 未覆盖，等待复核"
    return old_batch, ""


class PilotSync:
    def __init__(self, pilot, path):
        self.pilot = pilot
        self.store = pilot.store
        self.path = Path(path)
        self.log = self.store.root / "sync-status.json"
        self.trigger = threading.Event()
        self.stopping = threading.Event()
        self.last_inputs = {}

    def read(self):
        try:
            return json.loads(self.log.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"checked": "", "jobs": []}

    def write(self, status):
        pending = self.log.with_suffix(".tmp")
        pending.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        pending.replace(self.log)

    def user(self):
        with self.store.connection() as db:
            row = db.execute("SELECT id,name,role FROM users WHERE role='owner' AND enabled=1").fetchone()
        return dict(row) if row else None

    def inputs(self, kind, config):
        if kind == "metrics":
            return {"backup": read_stable(latest_file(config))}, ""
        if kind == "bugs" and config.get("mode") == "tp":
            return tp_snapshot(config)
        if kind == "bugs":
            file, cutoff = latest_file(config, dated=True)
            return {"snapshot": read_stable(file)}, cutoff
        files = {slot: Path(config["files"][slot]) for slot in IMPORT_FILES["pr"]}
        initial = {slot: (path.stat().st_mtime_ns, path.stat().st_size) for slot, path in files.items()}
        content = {slot: read_stable(path) for slot, path in files.items()}
        if initial != {slot: (path.stat().st_mtime_ns, path.stat().st_size) for slot, path in files.items()}:
            raise Problem("PR 工程正在更新一组文件，稍后重试")
        return content, config.get("version", VERSION)

    def job(self, user, kind, config, prepared=None):
        version = config.get("version", VERSION) if kind == "pr" else VERSION
        if prepared:
            inputs, digest, sources = prepared
            cutoff = version
        else:
            inputs, cutoff = self.inputs(kind, config)
            digest = self.digest(inputs)
            sources = None
        key = (kind, version)
        # API polling should not create two batches for unchanged records.
        marker = ("" if config.get("mode") == "tp" else cutoff, digest,
                  bool(config.get("publish")), bool(config.get("history")))
        if self.last_inputs.get(key, (None,))[0] == marker:
            result = self.last_inputs[key][1]
            if result.get("batch"):
                with self.store.connection() as db:
                    existing = db.execute("SELECT status FROM batches WHERE id=?", (result["batch"],)).fetchone()
                if existing and result["state"] != existing["status"] and existing["status"] in {"published", "withdrawn"}:
                    return {**result, "state": existing["status"], "message": "已由负责人处理"}
            return result
        result = {"kind": kind, "version": version if kind == "pr" else "", "source": [v[0] for v in inputs.values()]}
        try:
            normalized = cutoff_date(cutoff) if kind == "bugs" else "" if kind == "pr" and cutoff == VERSION else cutoff
            candidate = self.previous_input(kind, normalized, digest, config.get("mode") == "tp")
            if candidate:
                result.update(batch=candidate["id"], state=candidate["status"],
                              message="该来源批次已撤回，等待内容更新" if candidate["status"] == "withdrawn"
                              else "TP 快照内容与已有已发布批次一致")
                return result
            staged = self.pilot.stage(user, kind, cutoff, inputs, sources)
            result["batch"] = staged["id"]
            if staged["status"] == "published":
                result.update(state="published", message="已有同一已发布批次")
            else:
                expected, reason = hold_reason(self.store, user, kind, version, staged["id"], config.get("history", False))
                if reason or not config.get("publish"):
                    result.update(state="review", message=reason or "批次待负责人发布")
                else:
                    self.pilot.first_publish(user, {"id": staged["id"]}, expected_active=expected, check_active=True)
                    result.update(state="published", message="校验通过并已更新工作台")
        except Problem as exc:
            result.update(state="error", message=str(exc))
            if exc.status == 409:
                return result
        self.last_inputs[key] = (marker, result)
        return result

    @staticmethod
    def digest(inputs):
        return tuple(sorted((slot, hashlib.sha256(data).hexdigest()) for slot, (_, data) in inputs.items()))

    def record(self, user, jobs, previous, result):
        jobs.append(result)
        if result not in previous.get("jobs", []):
            with self.store.connection(True) as db:
                self.store.event(db, user["id"], "sync." + result["state"], result["kind"], result)

    def pr_range(self, user, settings, jobs, previous):
        try:
            files, _ = self.inputs("pr", settings)
            sources = pr_sources({slot: data for slot, (_, data) in files.items()})
            def version_key(value):
                if not re.fullmatch(r"\d{1,6}(?:\.\d{1,6}){2,3}", value):
                    raise ValueError("Version format")
                return tuple(map(int, value.split(".")))
            low, high = (version_key(settings[key]) for key in ("from", "to"))
            versions = [row["版本"] for row in sources[0] if low <= version_key(row["版本"]) <= high]
            if not versions or len(versions) != len(set(versions)):
                raise ValueError("Version manifest")
            prepared = (files, self.digest(files), sources)
        except (KeyError, TypeError, OSError, ValueError) as exc:
            message = str(exc) if isinstance(exc, Problem) else "历史 PR 范围或归档文件无法校验"
            self.record(user, jobs, previous, {"kind": "pr", "version": "", "state": "error", "message": message})
            return
        for version in versions:
            if version in settings.get("exclude", []):
                continue
            result = self.job(user, "pr", {**settings, "version": version, "history": True}, prepared)
            self.record(user, jobs, previous, result)

    def previous_input(self, kind, cutoff, digest, ignore_date):
        with self.store.connection() as db:
            if ignore_date:
                candidates = db.execute("SELECT id,status,manifest FROM batches WHERE kind=? "
                                        "AND status IN ('published','withdrawn') ORDER BY id DESC LIMIT 100", (kind,))
            else:
                candidates = db.execute("SELECT id,status,manifest FROM batches WHERE kind=? AND cutoff=? "
                                        "AND status='withdrawn' ORDER BY id DESC LIMIT 100", (kind, cutoff))
            return next((dict(row) for row in candidates if tuple(sorted(
                (slot, item["sha256"]) for slot, item in json.loads(row["manifest"]).items())) == digest), None)

    def run_once(self):
        config = json.loads(self.path.read_text(encoding="utf-8-sig"))
        previous = self.read()
        user = self.user()
        jobs = []
        if user:
            for kind in ("metrics", "pr", "bugs"):
                streams = config.get(kind, [])
                if isinstance(streams, dict):
                    streams = [streams]
                for stream in streams:
                    if not stream.get("enabled", True):
                        continue
                    try:
                        result = self.job(user, kind, stream)
                    except (KeyError, TypeError, OSError, ValueError) as exc:
                        message = str(exc) if isinstance(exc, Problem) else "来源配置无效或文件暂不可读"
                        result = {"kind": kind, "version": stream.get("version", "") if kind == "pr" else "",
                                  "state": "error", "message": message}
                    self.record(user, jobs, previous, result)
                if kind == "pr" and config.get("pr_history", {}).get("enabled", False):
                    self.pr_range(user, config["pr_history"], jobs, previous)
        self.write({"checked": now(), "enabled": True, "jobs": jobs})
        return max(60, min(3600, int(config.get("interval_seconds", 300))))

    def worker(self):
        while not self.stopping.is_set():
            self.trigger.clear()
            try:
                interval = self.run_once()
            except (OSError, ValueError, TypeError, AttributeError):
                self.write({"checked": now(), "enabled": True, "jobs": [],
                            "error": "自动更新配置无法读取"})
                interval = 300
            self.trigger.wait(interval)
