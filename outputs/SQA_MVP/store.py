"""Transactional state, evidence provenance, and pilot authorization."""

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from imports import IMPORT_FILES, VERSION, InvalidImport, parse

try:
    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    LOCAL_TZ = timezone(timedelta(hours=8))

ROLES = {"owner", "reviewer", "member"}
MAX_FILE = 8 * 1024 * 1024
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, role TEXT NOT NULL,
    salt TEXT NOT NULL, password TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS one_owner ON users(role) WHERE role = 'owner';
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, cutoff TEXT NOT NULL, fingerprint TEXT NOT NULL,
    status TEXT NOT NULL, manifest TEXT NOT NULL, payload TEXT, error TEXT,
    who INTEGER NOT NULL REFERENCES users(id), created TEXT NOT NULL, published TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_valid_batch
    ON batches(fingerprint) WHERE status IN ('staged','published');
CREATE TABLE IF NOT EXISTS metrics (
    metric_id TEXT PRIMARY KEY, target TEXT NOT NULL, approval TEXT NOT NULL,
    actual TEXT NOT NULL, evidence TEXT NOT NULL, lamp TEXT NOT NULL, changed TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pr_reviews (
    batch INTEGER NOT NULL REFERENCES batches(id), entry TEXT NOT NULL, rule TEXT NOT NULL,
    result TEXT NOT NULL, evidence TEXT NOT NULL, who INTEGER NOT NULL REFERENCES users(id),
    changed TEXT NOT NULL, PRIMARY KEY(batch,entry,rule)
);
CREATE TABLE IF NOT EXISTS bug_reviews (
    batch INTEGER NOT NULL REFERENCES batches(id), bug_id TEXT NOT NULL,
    result TEXT NOT NULL, evidence TEXT NOT NULL, who INTEGER NOT NULL REFERENCES users(id),
    changed TEXT NOT NULL, PRIMARY KEY(batch,bug_id)
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, batch INTEGER NOT NULL REFERENCES batches(id),
    source_key TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL, due TEXT NOT NULL,
    assignee INTEGER NOT NULL REFERENCES users(id), verifier INTEGER REFERENCES users(id),
    state TEXT NOT NULL, verified_by INTEGER REFERENCES users(id), created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, actor INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL, subject TEXT NOT NULL, detail TEXT NOT NULL, created TEXT NOT NULL
);
"""


class Problem(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def need(condition, message, status=400):
    if not condition:
        raise Problem(message, status)


def now():
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def encode(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def clean(data, key, maximum=2000, required=True):
    value = data.get(key, "")
    need(isinstance(value, str), f"{key} 格式无效")
    value = value.strip()
    need(len(value) <= maximum and (value or not required), f"{key} 为空或超出长度限制")
    return value


def number(value, label):
    need(type(value) is int and value > 0, f"{label} 无效")
    return value


def day(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise Problem("日期须为 YYYY-MM-DD") from exc


def cutoff_date(value):
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return day(value)
        need(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\+08:00)?", value),
             "截止时间须填写北京时间")
        return datetime.fromisoformat(value).replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
    except ValueError as exc:
        raise Problem("截止时间格式无效") from exc


def owner(user):
    need(user["role"] == "owner", "此操作仅对试点负责人开放", 403)


def reviewer(user):
    need(user["role"] in {"owner", "reviewer"}, "此操作需要证据复核权限", 403)


def password_hash(secret, salt):
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), 600_000).hex()


class Store:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.sources = self.root / "sources"
        self.sources.mkdir(parents=True, exist_ok=True)
        self.db_file = self.root / "pilot.sqlite3"
        with closing(sqlite3.connect(self.db_file, timeout=10)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.db_file, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, actor, action, subject, detail):
        db.execute("INSERT INTO events(actor,action,subject,detail,created) VALUES(?,?,?,?,?)",
                   (actor, action, subject, encode(detail), now()))

    def setup_needed(self):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM users WHERE role='owner'").fetchone() is None

    def add_user(self, actor, data):
        name = clean(data, "name", 64)
        password = clean(data, "password", 128)
        role = clean(data, "role", 12)
        need(len(password) >= 11, "密码至少 11 个字符")
        need(role in ROLES, "角色无效")
        salt = secrets.token_hex(16)
        digest = password_hash(password, salt)
        with self.connection(True) as db:
            if actor is None:
                need(role == "owner" and not db.execute("SELECT 1 FROM users").fetchone(),
                     "初始负责人已存在", 409)
            else:
                owner(actor)
                need(role != "owner", "试点仅设一位负责人")
            need(db.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone() is None,
                 "账号名称已存在", 409)
            uid = db.execute("INSERT INTO users(name,role,salt,password) VALUES(?,?,?,?)",
                             (name, role, salt, digest)).lastrowid
            self.event(db, actor["id"] if actor else uid, "account.create", str(uid), {"role": role})
        return {"id": uid, "name": name, "role": role}

    def set_password(self, actor, data):
        """Set credentials during authorized maintenance with an audit record.

        The caller must revoke sessions by restarting the server or signing the
        affected account in on each active server after the password change.
        """
        owner(actor)
        uid = number(data.get("id"), "账号")
        password = clean(data, "password", 128)
        need(len(password) >= 11, "密码至少 11 个字符")
        salt = secrets.token_hex(16)
        digest = password_hash(password, salt)
        with self.connection(True) as db:
            count = db.execute("UPDATE users SET salt=?,password=? WHERE id=?",
                               (salt, digest, uid)).rowcount
            need(count == 1, "账号不存在", 404)
            self.event(db, actor["id"], "account.password.reset", str(uid), {})
        return {"id": uid}

    def login(self, data):
        name = clean(data, "name", 64)
        password = clean(data, "password", 128)
        with self.connection() as db:
            row = db.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        candidate = password_hash(password, row["salt"] if row else "0" * 32)
        need(row and secrets.compare_digest(candidate, row["password"]) and row["enabled"],
             "账号或密码无效", 401)
        return {key: row[key] for key in ("id", "name", "role")}

    def get_user(self, uid):
        with self.connection() as db:
            row = db.execute("SELECT id,name,role FROM users WHERE id=? AND enabled=1", (uid,)).fetchone()
        return dict(row) if row else None

    def set_account(self, actor, data):
        owner(actor)
        uid = number(data.get("id"), "账号")
        enabled = data.get("enabled")
        need(type(enabled) is bool and uid != actor["id"], "无法更改此账号")
        with self.connection(True) as db:
            count = db.execute("UPDATE users SET enabled=? WHERE id=? AND role<>'owner'",
                               (int(enabled), uid)).rowcount
            need(count == 1, "账号不存在", 404)
            self.event(db, actor["id"], "account.status", str(uid), {"enabled": enabled})
        return {"id": uid, "enabled": enabled}

    def accounts(self, user):
        reviewer(user)
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT id,name,role,enabled FROM users ORDER BY id")]

    def stage_import(self, user, kind, cutoff, files, prepared_pr=None):
        owner(user)
        need(kind in IMPORT_FILES and set(files) == set(IMPORT_FILES[kind]), "请选择该来源的完整文件")
        if kind == "bugs":
            cutoff = cutoff_date(cutoff)
        elif kind == "pr":
            cutoff = "" if not cutoff or cutoff == VERSION else cutoff
            need(not cutoff or re.fullmatch(r"\d{1,6}(?:\.\d{1,6}){2,3}", cutoff), "PR 版本号格式无效")
        else:
            cutoff = ""
        manifest = {}
        for slot, (name, data) in files.items():
            need(len(data) <= MAX_FILE, "单个来源文件超出 8 MiB")
            manifest[slot] = {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        identity = [kind, cutoff, "pilot-mapping-1", sorted((k, v["sha256"]) for k, v in manifest.items())]
        fingerprint = hashlib.sha256(encode(identity).encode()).hexdigest()
        with self.connection() as db:
            prior = db.execute("SELECT id,status,payload FROM batches WHERE fingerprint=? AND status IN ('staged','published')",
                               (fingerprint,)).fetchone()
        if prior:
            return {"id": prior["id"], "status": prior["status"], "duplicate": True,
                    "summary": summarize(kind, json.loads(prior["payload"]))}
        try:
            payload = parse(kind, {k: f[1] for k, f in files.items()}, cutoff, prepared_pr)
        except InvalidImport as exc:
            with self.connection(True) as db:
                failed_id = db.execute(
                    "INSERT INTO batches(kind,cutoff,fingerprint,status,manifest,error,who,created)"
                    " VALUES(?,?,?,'failed',?,?,?,?)",
                    (kind, cutoff, fingerprint, encode(manifest), str(exc), user["id"], now())).lastrowid
                self.event(db, user["id"], "import.failed", str(failed_id), {"reason": str(exc)})
            raise Problem(f"{exc}（失败批次 #{failed_id}）") from exc
        with self.connection(True) as db:
            prior = db.execute("SELECT id,status FROM batches WHERE fingerprint=? AND status IN ('staged','published')",
                               (fingerprint,)).fetchone()
            if prior:
                return {**dict(prior), "duplicate": True, "summary": summarize(kind, payload)}
            for slot, (_, data) in files.items():
                dest = self.sources / manifest[slot]["sha256"]
                if not dest.exists():
                    temporary = self.sources / (dest.name + "." + secrets.token_hex(8) + ".tmp")
                    try:
                        temporary.write_bytes(data)
                        temporary.replace(dest)
                    finally:
                        temporary.unlink(missing_ok=True)
            batch_id = db.execute(
                "INSERT INTO batches(kind,cutoff,fingerprint,status,manifest,payload,who,created)"
                " VALUES(?,?,?,'staged',?,?,?,?)",
                (kind, cutoff, fingerprint, encode(manifest), encode(payload), user["id"], now())).lastrowid
            self.event(db, user["id"], "import.stage", str(batch_id), {"kind": kind})
        return {"id": batch_id, "status": "staged", "duplicate": False, "summary": summarize(kind, payload)}

    def list_batches(self, user):
        owner(user)
        with self.connection() as db:
            rows = db.execute("SELECT id,kind,cutoff,status,error,created,published,manifest,payload"
                              " FROM batches ORDER BY id DESC LIMIT 100").fetchall()
        return [{**{k: row[k] for k in row.keys() if k not in {"manifest", "payload"}},
                 "manifest": json.loads(row["manifest"]),
                 "summary": summarize(row["kind"], json.loads(row["payload"])) if row["payload"] else None}
                for row in rows]

    def publish(self, user, data, expected_active=None, check_active=False):
        owner(user)
        batch = number(data.get("id"), "批次")
        with self.connection(True) as db:
            entry = db.execute("SELECT status,kind,cutoff FROM batches WHERE id=?", (batch,)).fetchone()
            need(entry and entry["status"] in {"staged", "published"}, "批次不可发布", 409)
            if entry["status"] == "staged":
                if check_active:
                    version = entry["cutoff"] if entry["kind"] == "pr" and entry["cutoff"] else VERSION
                    active_id, _ = self.active(db, entry["kind"], version)
                    need(active_id == expected_active, "当前批次已变化，稍后重新校验", 409)
                db.execute("UPDATE batches SET status='published',published=? WHERE id=?", (now(), batch))
                self.event(db, user["id"], "import.publish", str(batch), {"kind": entry["kind"]})
        return {"id": batch, "status": "published"}

    def withdraw(self, user, data):
        owner(user)
        batch = number(data.get("id"), "批次")
        reason = clean(data, "reason", 500)
        with self.connection(True) as db:
            changed = db.execute("UPDATE batches SET status='withdrawn' WHERE id=? AND status='published'",
                                 (batch,)).rowcount
            need(changed == 1, "只有已发布批次可撤回", 409)
            self.event(db, user["id"], "import.withdraw", str(batch), {"reason": reason})
        return {"id": batch, "status": "withdrawn"}

    def source_download(self, user, batch, slot):
        owner(user)
        with self.connection() as db:
            row = db.execute("SELECT manifest,payload FROM batches WHERE id=?", (batch,)).fetchone()
        manifest = json.loads(row["manifest"]) if row and row["payload"] else {}
        need(slot in manifest, "找不到来源文件", 404)
        item = manifest[slot]
        path = self.sources / item["sha256"]
        need(path.is_file(), "来源文件待恢复", 404)
        return item["name"], path.read_bytes()

    def active(self, db, kind, version=VERSION):
        order = "cutoff DESC,published DESC,id DESC" if kind == "bugs" else "published DESC,id DESC"
        version_filter = " AND cutoff=?" if kind == "pr" else ""
        parameters = (kind, "" if version == VERSION else version) if kind == "pr" else (kind,)
        row = db.execute("SELECT id,payload FROM batches WHERE kind=? AND status='published'" + version_filter
                         + f" ORDER BY {order} LIMIT 1", parameters).fetchone()
        return (row["id"], json.loads(row["payload"])) if row else (None, None)

    def pr_versions(self, db):
        values = [row[0] or VERSION for row in db.execute(
            "SELECT DISTINCT cutoff FROM batches WHERE kind='pr' AND status='published'")]
        return sorted(values, key=lambda version: tuple(map(int, version.split("."))), reverse=True)

    def pr_overview(self, user):
        reviewer(user)
        with self.connection() as db:
            rows = []
            unique_prs = set()
            for version in self.pr_versions(db):
                batch, payload = self.active(db, "pr", version)
                entries = payload["entries"]
                found_prs = {entry["pr_id"] for entry in entries if entry["pr_id"]}
                unique_prs.update(found_prs)
                matched = sum(entry["match"]["match_status"] == "MATCHED" for entry in entries)
                ambiguous = sum(entry["match"]["match_status"] == "AMBIGUOUS" for entry in entries)
                unmatched = len(entries) - matched - ambiguous
                rows.append({"version": version, "batch": batch, "date": payload["summary"]["发布日期"],
                             "count": len(entries), "matched": matched, "ambiguous": ambiguous,
                             "unmatched": unmatched, "audit": payload["audit_count"],
                             "categories": {key: int(payload["summary"].get(key) or 0)
                                            for key in ("New Features", "Bugfixes", "Technical", "Testing")}})
        totals = {key: sum(row[key] for row in rows) for key in ("count", "matched", "ambiguous", "unmatched", "audit")}
        return {"rows": rows, "totals": {**totals, "versions": len(rows), "unique_prs": len(unique_prs)}}

    def source_context(self, db, kind, batch):
        if kind == "pr":
            row = db.execute("SELECT cutoff FROM batches WHERE id=? AND kind='pr' AND status='published'",
                             (batch,)).fetchone()
            need(row, "PR 来源未发布", 409)
            return self.active(db, kind, row["cutoff"] or VERSION)
        return self.active(db, kind)

    def dashboard(self, user):
        with self.connection() as db:
            metrics_id, metrics = self.active(db, "metrics")
            pr_id, prs = self.active(db, "pr")
            bug_id, bugs = self.active(db, "bugs")
            task_where = " WHERE assignee=? OR verifier=?" if user["role"] == "member" else ""
            args = (user["id"], user["id"]) if task_where else ()
            tasks = db.execute("SELECT state,COUNT(*) AS total FROM tasks" + task_where + " GROUP BY state", args)
            filled = db.execute("SELECT COUNT(*) FROM metrics WHERE actual<>''").fetchone()[0]
            return {"metric_count": len(metrics["items"]) if metrics else None,
                    "snapshot_count": len(metrics["snapshots"]) if metrics else None,
                    "filled": filled, "pr_count": len(prs["entries"]) if prs else None,
                    "pr_rules": prs["audit_count"] if prs else None,
                    "bugs": bugs["stats"] if bugs else None, "bug_cutoff": bugs["cutoff"] if bugs else None,
                    "batches": {"metrics": metrics_id, "pr": pr_id, "bugs": bug_id},
                    "tasks": {row["state"]: row["total"] for row in tasks}}

    def detail(self, user, kind, version=VERSION):
        reviewer(user)
        with self.connection() as db:
            batch, payload = self.active(db, kind, version)
            context = {"versions": self.pr_versions(db), "version": version} if kind == "pr" else {}
            if not payload:
                return {"batch": None, "payload": None, **context}
            columns = {"metrics": ("metrics", "metric_id"), "pr": ("pr_reviews", "entry"),
                       "bugs": ("bug_reviews", "bug_id")}
            table, key = columns[kind]
            if kind == "metrics":
                reviews = {row[key]: dict(row) for row in db.execute("SELECT * FROM metrics")}
            else:
                reviews = [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE batch=?", (batch,))]
        return {"batch": batch, "payload": payload, "reviews": reviews, **context}

    def save_metric(self, user, data):
        owner(user)
        metric_id = clean(data, "id", 100)
        fields = {key: clean(data, key, 2000, False)
                  for key in ("target", "approval", "actual", "evidence", "lamp")}
        need(fields["lamp"] in {"", "green", "yellow", "red"}, "灯色不支持")
        need(fields["lamp"] != "green", "绿色判定请通过口径与证据独立复核")
        if fields["lamp"]:
            need(all(fields[key] for key in ("target", "approval", "actual", "evidence")),
                 "缺少目标、目标批准凭据、实绩或证据，本次不能判定灯色")
        with self.connection(True) as db:
            _, payload = self.active(db, "metrics")
            need(payload and any(row["id"] == metric_id for row in payload["items"]), "指标不存在", 404)
            prior = db.execute("SELECT * FROM metrics WHERE metric_id=?", (metric_id,)).fetchone()
            db.execute("INSERT INTO metrics VALUES(?,?,?,?,?,?,?) ON CONFLICT(metric_id) DO UPDATE SET"
                       " target=excluded.target,approval=excluded.approval,actual=excluded.actual,"
                       " evidence=excluded.evidence,lamp=excluded.lamp,changed=excluded.changed",
                       (metric_id, *(fields[k] for k in ("target", "approval", "actual", "evidence", "lamp")), now()))
            self.event(db, user["id"], "metric.save", metric_id, {"before": dict(prior) if prior else None, "after": fields})
        return {"id": metric_id}

    def review(self, user, data, kind):
        reviewer(user)
        batch = number(data.get("batch"), "批次")
        key = clean(data, "key", 100)
        result = clean(data, "result", 30)
        evidence = clean(data, "evidence")
        need(result in {"待补证", "继续跟踪", "已核对"}, "复核状态不支持")
        with self.connection(True) as db:
            active_batch, payload = self.source_context(db, kind, batch)
            need(batch == active_batch, "数据已变更，请刷新后复核", 409)
            if kind == "pr":
                rule = clean(data, "rule", 30)
                need(any(row["key"] == key and any(r["rule_id"] == rule for r in row["rules"])
                         for row in payload["entries"]), "PR 规则不存在", 404)
                db.execute("INSERT INTO pr_reviews VALUES(?,?,?,?,?,?,?) ON CONFLICT(batch,entry,rule)"
                           " DO UPDATE SET result=excluded.result,evidence=excluded.evidence,who=excluded.who,"
                           " changed=excluded.changed", (batch, key, rule, result, evidence, user["id"], now()))
            else:
                rule = ""
                need(any(row["ID"] == key for row in payload["rows"]), "缺陷不存在", 404)
                db.execute("INSERT INTO bug_reviews VALUES(?,?,?,?,?,?) ON CONFLICT(batch,bug_id)"
                           " DO UPDATE SET result=excluded.result,evidence=excluded.evidence,who=excluded.who,"
                           " changed=excluded.changed", (batch, key, result, evidence, user["id"], now()))
            self.event(db, user["id"], kind + ".review", f"{batch}:{key}:{rule}",
                       {"result": result, "evidence": evidence})
        return {"key": key}

    def create_task(self, user, data):
        owner(user)
        kind = clean(data, "kind", 10)
        need(kind in {"pr", "bugs", "metrics"}, "发现来源不支持")
        batch = number(data.get("batch"), "来源批次")
        key = clean(data, "source_key", 100)
        title = clean(data, "title", 200)
        detail = clean(data, "detail")
        due = day(clean(data, "due", 10))
        assignee = number(data.get("assignee"), "整改负责人")
        verifier = number(data.get("verifier"), "验证人")
        need(assignee != verifier, "整改负责人和独立验证人需为不同账号")
        with self.connection(True) as db:
            active_id, payload = self.source_context(db, kind, batch)
            need(batch == active_id, "来源批次已变化，请刷新", 409)
            rows, id_key = {"pr": ("entries", "key"), "bugs": ("rows", "ID"), "metrics": ("items", "id")}[kind]
            need(any(row[id_key] == key for row in payload[rows]), "来源记录不存在", 404)
            self.check_users(db, [assignee, verifier])
            uid = db.execute("INSERT INTO tasks(kind,batch,source_key,title,detail,due,assignee,verifier,state,created)"
                             " VALUES(?,?,?,?,?,?,?,?,'open',?)",
                             (kind, batch, key, title, detail, due, assignee, verifier, now())).lastrowid
            self.event(db, user["id"], "task.create", str(uid), {"assignee": assignee, "verifier": verifier})
        return {"id": uid}

    def check_users(self, db, uids):
        need(all(db.execute("SELECT 1 FROM users WHERE id=? AND enabled=1", (uid,)).fetchone()
                 for uid in uids), "所选账号不存在或已停用")

    def contributors(self, db, task_id):
        events = db.execute("SELECT actor,detail FROM events WHERE action='task.submit' AND subject=?", (str(task_id),))
        result = set()
        for event in events:
            result.add(event["actor"])
            result.update(json.loads(event["detail"])["contributors"])
        return result

    def task_action(self, user, data):
        task_id = number(data.get("id"), "整改任务")
        action = clean(data, "action", 20)
        note = clean(data, "note")
        uid = user["id"]
        with self.connection(True) as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            need(task, "任务不存在", 404)
            need(user["role"] in {"owner", "reviewer"} or uid in (task["assignee"], task["verifier"]),
                 "你未被分派此任务", 403)
            detail = {"note": note}
            state = task["state"]
            contributors = self.contributors(db, task_id)
            if action == "submit":
                need(uid == task["assignee"], "仅被分派的整改负责人可以提交证据", 403)
                need(state == "open", "当前状态不可提交证据", 409)
                coauthors = data.get("contributors", [])
                need(isinstance(coauthors, list) and len(coauthors) <= 10, "共同提交人格式无效")
                coauthors = sorted({number(i, "共同提交人") for i in coauthors} - {uid})
                self.check_users(db, coauthors)
                detail["contributors"] = coauthors
                db.execute("UPDATE tasks SET state='submitted',verified_by=NULL WHERE id=?", (task_id,))
                state = "submitted"
            elif action in {"verify", "reject"}:
                need(uid == task["verifier"] and uid not in contributors and uid != task["assignee"],
                     "仅指定且未参与整改提交的验证人可以验证", 403)
                need(state == "submitted", "需先提交整改证据", 409)
                state = "verified" if action == "verify" else "open"
                db.execute("UPDATE tasks SET state=?,verified_by=? WHERE id=?",
                           (state, uid if action == "verify" else None, task_id))
            elif action == "assign_verifier":
                owner(user)
                need(state in {"open", "submitted"}, "当前状态不可改派验证人", 409)
                target = number(data.get("verifier"), "验证人")
                self.check_users(db, [target])
                need(target not in contributors and target != task["assignee"], "验证人须独立于整改提交")
                detail.update({"before": task["verifier"], "after": target})
                db.execute("UPDATE tasks SET verifier=? WHERE id=?", (target, task_id))
            elif action == "close":
                owner(user)
                need(state == "verified" and task["verified_by"] and task["verified_by"] not in contributors,
                     "需有通过的独立验证才能关闭", 409)
                db.execute("UPDATE tasks SET state='closed' WHERE id=?", (task_id,))
                state = "closed"
            else:
                raise Problem("未知整改操作")
            self.event(db, uid, "task." + action, str(task_id), detail)
        return {"id": task_id, "state": state}

    def tasks(self, user):
        with self.connection() as db:
            query = ("SELECT t.*,a.name AS assignee_name,v.name AS verifier_name,b.status AS source_status "
                     "FROM tasks t JOIN users a ON a.id=t.assignee LEFT JOIN users v ON v.id=t.verifier "
                     "JOIN batches b ON b.id=t.batch")
            args = ()
            if user["role"] == "member":
                query += " WHERE t.assignee=? OR t.verifier=?"
                args = (user["id"], user["id"])
            tasks = []
            for row in db.execute(query + " ORDER BY t.id DESC", args):
                history = db.execute("SELECT e.action,e.detail,e.created,u.name FROM events e JOIN users u ON u.id=e.actor"
                                     " WHERE e.subject=? AND e.action LIKE 'task.%' ORDER BY e.id", (str(row["id"]),))
                tasks.append({**dict(row), "contributors": sorted(self.contributors(db, row["id"])),
                              "history": [{**dict(event), "detail": json.loads(event["detail"])} for event in history]})
            return tasks

    def task_people(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT id,name FROM users WHERE enabled=1 ORDER BY id")]

    def audit(self, user):
        owner(user)
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT e.action,e.subject,e.detail,e.created,u.name "
                                                   "FROM events e JOIN users u ON u.id=e.actor ORDER BY e.id DESC LIMIT 200")]


def summarize(kind, payload):
    if kind == "metrics":
        return {"配置记录": len(payload["items"]),
                **{snapshot["label"]: len(snapshot["rows"]) for snapshot in payload["snapshots"]}}
    if kind == "pr":
        return {"发布条目": len(payload["entries"]),
                "唯一匹配条目": sum(row["match"]["match_status"] == "MATCHED" for row in payload["entries"]),
                "逐规则明细": payload["audit_count"]}
    return {"原始行": payload["stats"]["raw_total"], "RID 有效缺陷": payload["stats"]["effective"],
            "RID 研发待解决": payload["stats"]["pending"], "RID 待验证": payload["stats"]["testing"]}
