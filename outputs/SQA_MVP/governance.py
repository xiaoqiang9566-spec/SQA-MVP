"""Controlled metric evidence, document revisions and project responsibilities."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from store import MAX_FILE, clean, cutoff_date, encode, need, now, number, owner, reviewer

STAFF = {
    "曾建芬": (["Race3S", "Race3"], ["质量体系建设专项"]),
    "肖强": (["Chitu", "NG3-Q3"], ["Bug专项-消减/售后"]),
    "夏春燕": (["Flex"], ["Feature看护专项"]),
    "谢如菊": (["Ocean2", "SHRM2", "Run2-NIO"], []),
    "薛小玉": (["Core2", "Vertical3", "Vertical3S"], []),
    "杜楠楠": (["SU11"], ["FT smoke专项"]),
    "蒋青霞": (["Dilu", "NG3-Q3"], []),
    "王程": (["Vertical3", "Vertical3S"], []),
    "陈天忍": (["Vertical3", "Vertical3S"], ["Daily看护专项"]),
    "段郁敏": ([], ["FT smoke专项", "Feature看护专项"]),
}
DOC_ROOT = Path("D:/6208/Documents/质量管理/docs/quality-system/processes")
DOC_NAMES = ["quality-documented-information-control-spec.md", "quality-capa-and-corrective-action-spec.md",
             "quality-management-review-and-competence-spec.md", "company-qes-software-quality-adaptation-guide.md",
             "quality-system-requirements-coverage-matrix.md", "project-module-quality-owner-matrix.md",
             "phase-entry-exit-master-matrix.md", "stage-gate-review-spec.md",
             "project-quality-dashboard-governance-spec.md", "end-to-end-process-data-dictionary.md",
             "tp-bug-lifecycle-management-spec.md", "bitbucket-development-review-spec.md",
             "release-management-spec.md", "software-project-quality-management-spec.md"]
SCHEMA = """
CREATE TABLE IF NOT EXISTS responsibilities (
 user_id INTEGER PRIMARY KEY REFERENCES users(id), projects TEXT NOT NULL, specialties TEXT NOT NULL,
 revision INTEGER NOT NULL, changed TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_records (
 id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, project TEXT NOT NULL, version TEXT NOT NULL,
 stage TEXT NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
 state TEXT NOT NULL, revision INTEGER NOT NULL, editor INTEGER NOT NULL REFERENCES users(id),
 submitter INTEGER REFERENCES users(id), reviewer INTEGER REFERENCES users(id), changed TEXT NOT NULL,
 UNIQUE(source_id,project,version,stage));
CREATE TABLE IF NOT EXISTS controlled_documents (
 id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, state TEXT NOT NULL,
 revision INTEGER NOT NULL, editor INTEGER NOT NULL REFERENCES users(id),
 submitter INTEGER REFERENCES users(id), approver INTEGER REFERENCES users(id), changed TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS controlled_document_history (
 document_id INTEGER NOT NULL REFERENCES controlled_documents(id), revision INTEGER NOT NULL,
 payload TEXT NOT NULL, state TEXT NOT NULL, actor INTEGER NOT NULL REFERENCES users(id), changed TEXT NOT NULL,
 PRIMARY KEY(document_id,revision));
CREATE TABLE IF NOT EXISTS governance_blobs (sha256 TEXT PRIMARY KEY);
"""


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def canonical(project):
    return {"Race3（归属待确认）": "Race3", "Chitu NIO": "Chitu"}.get(project, project)


class Governance:
    def __init__(self, store, library=None):
        self.store = store
        self.library = library
        with store.connection(True) as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            for row in db.execute("SELECT id,name FROM users"):
                if row["name"] in STAFF:
                    projects, specialties = STAFF[row["name"]]
                    db.execute("INSERT OR IGNORE INTO responsibilities VALUES(?,?,?,?,?)",
                               (row["id"], encode(projects), encode(specialties), 1, now()))

    def initialize_drafts(self, user):
        owner(user)
        existing_docs = {row["code"] for row in self.documents(user)["documents"]}
        existing_metrics = {(row["source_id"], row["version"]) for row in self.records(user)["records"]}
        documents = metrics = 0
        for name in DOC_NAMES:
            code = "QS-" + Path(name).stem
            path = DOC_ROOT / name
            if code in existing_docs or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8-sig")
            title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("# ")), name)
            self.document_save(user, {"code": code, "revision": 0, "title": title, "version": "待确认",
                                     "owner": "待确认", "source_key": name, "retention": "待确认",
                                     "note": "首批受控候选登记，待核对有效版本与批准凭据"})
            documents += 1
        for source in self.catalog(user)["items"]:
            if not source["source_id"].startswith("pilot-") or (source["source_id"], "2.57.64") in existing_metrics:
                continue
            self.evidence_save(user, {"source_id": source["source_id"], "version": "2.57.64", "revision": 0,
                                      "formula": source["formula"], "target": source["target"],
                                      "actual": source["actual"], "evidence": source["evidence"],
                                      "scope": "Race 3S / 2.57.64，构建归属与统计样本范围待补证",
                                      "rule_version": "待确认", "lamp": ""})
            metrics += 1
        return {"documents_seeded": documents, "metric_drafts_seeded": metrics}

    def metric_detail(self, user):
        data = self.store.detail(user, "metrics")
        records = self.records(user)["records"]
        for key, row in data.get("reviews", {}).items():
            approved = next((r for r in records if r["source_id"] == "pilot-" + key and r["version"] == "2.57.64"
                             and r["effective_state"] == "verified"), None)
            row["source_lamp"] = row["lamp"]
            row["lamp"] = approved["effective_lamp"] if approved else ""
            row["review_status"] = "已独立复核" if approved else "待复核"
        return data

    def dashboard(self, user):
        data = self.store.dashboard(user)
        if not self.allowed(user, "Race3S"):
            data.update({"metric_count": None, "snapshot_count": None, "filled": 0,
                         "pr_count": None, "pr_rules": None, "bugs": None, "bug_cutoff": None,
                         "batches": {"metrics": None, "pr": None, "bugs": None}})
        records = self.records(user)["records"]
        data["evidence_review"] = {state: sum(r["effective_state"] == state for r in records)
                                   for state in ("draft", "submitted", "verified", "rejected", "stale")}
        return data

    def grants(self, user):
        with self.store.connection() as db:
            row = db.execute("SELECT projects FROM responsibilities WHERE user_id=?", (user["id"],)).fetchone()
        return json.loads(row[0]) if row else []

    def allowed(self, user, project):
        return user["role"] in {"owner", "reviewer"} or canonical(project) in self.grants(user)

    def board(self, user, full=False):
        need(self.library, "项目工程尚未配置", 404)
        data = json.loads(encode(self.library.project_board(True)))
        projects = [p for p in data.get("projects", []) if self.allowed(user, p["name"])]
        result = {**data, "projects": projects}
        if not any(p["name"] == "Race3S" for p in projects):
            result["stage_reviews"] = {}
        records = self.records(user)["records"]
        for project in projects:
            from collections import Counter
            project["counts"] = dict(Counter(r["stage"] for r in project["rows"]))
            project["lamps"] = dict(Counter(r["source_lamp"] for r in project["rows"]))
            project["with_evidence"] = sum(bool(r["evidence"]) for r in project["rows"])
            project["metrics"] = len(project["rows"])
            for row in project["rows"]:
                identity, fingerprint = self.row_source(project, row)
                matches = [r for r in records if r["source_id"] == identity]
                row["evidence_records"] = matches
                row["source_lamp_original"] = row["source_lamp"]
                row["source_lamp"] = ""  # Source lamps require a version-specific independent review.
                row["review_status"] = "按版本查看复核记录" if matches else "待复核"
        return result

    @staticmethod
    def row_source(project, row):
        identity = "board-" + digest([project["name"], row["stage"], row["name"], row["definition"], row.get("source"), row.get("node")])
        snapshot = {"project": project["name"], "row": row}
        # Exclude presentation fields added by this module.
        snapshot["row"] = {k: v for k, v in row.items() if k not in {"evidence_records", "source_lamp_original", "review_status"}}
        return identity, digest(snapshot)

    def catalog(self, user):
        items = []
        if self.library:
            board = self.library.project_board(True)
            for project in board.get("projects", []):
                if not self.allowed(user, project["name"]):
                    continue
                for row in project["rows"]:
                    identity, fingerprint = self.row_source(project, row)
                    items.append({"source_id": identity, "project": project["name"], "stage": row["stage"],
                                  "name": row["name"], "formula": row["definition"], "target": row["target"],
                                  "actual": row["actual"], "evidence": row["evidence"], "source": row["source"],
                                  "cutoff": row.get("snapshot", ""), "fingerprint": fingerprint,
                                  "snapshot": {"project": project["name"], "row": row}})
        with self.store.connection() as db:
            batch, payload = self.store.active(db, "metrics")
            if payload and self.allowed(user, "Race3S"):
                saved = {r["metric_id"]: dict(r) for r in db.execute("SELECT * FROM metrics")}
                for row in payload["items"]:
                    current = saved.get(row["id"], {})
                    snapshot = {"batch": batch, "item": row, "saved": current}
                    items.append({"source_id": "pilot-" + row["id"], "project": "Race3S", "stage": "RID",
                                  "name": row.get("name", row["id"]), "formula": row.get("definition", ""),
                                  "target": current.get("target") or row.get("target", ""), "actual": current.get("actual", ""),
                                  "evidence": current.get("evidence", ""), "source": f"导入批次 {batch}", "cutoff": "",
                                  "fingerprint": digest(snapshot), "snapshot": snapshot})
        unique = {}
        for item in items:
            need(item["source_id"] not in unique, "工程存在重复指标标识，需先核对源表", 409)
            unique[item["source_id"]] = item
        return {"items": items}

    def records(self, user):
        catalog = {r["source_id"]: r for r in self.catalog(user)["items"]}
        with self.store.connection() as db:
            rows = db.execute("SELECT r.*,u.name editor_name,s.name submitter_name,v.name reviewer_name FROM evidence_records r "
                              "JOIN users u ON u.id=r.editor LEFT JOIN users s ON s.id=r.submitter LEFT JOIN users v ON v.id=r.reviewer ORDER BY r.id DESC").fetchall()
        result = []
        for row in rows:
            if not self.allowed(user, row["project"]):
                continue
            data = dict(row)
            data["payload"] = json.loads(data["payload"])
            current = catalog.get(row["source_id"])
            data["effective_state"] = row["state"] if current and current["fingerprint"] == row["fingerprint"] else "stale"
            data["effective_lamp"] = data["payload"].get("lamp", "") if data["effective_state"] == "verified" else ""
            result.append(data)
        return {"records": result}

    def blob(self, db, raw):
        need(len(raw) <= MAX_FILE, "证据文件超过容量限制")
        sha = hashlib.sha256(raw).hexdigest()
        target = self.store.sources / sha
        if not target.exists():
            temporary = self.store.sources / (sha + ".tmp")
            temporary.write_bytes(raw)
            temporary.replace(target)
        need(hashlib.sha256(target.read_bytes()).hexdigest() == sha, "证据快照校验失败", 409)
        db.execute("INSERT OR IGNORE INTO governance_blobs VALUES(?)", (sha,))
        return sha

    @staticmethod
    def revision(data, current):
        need(type(data.get("revision")) is int and data["revision"] == (current["revision"] if current else 0),
             "记录已变更，请刷新后重试", 409)

    def evidence_save(self, user, data):
        source_id = clean(data, "source_id", 100)
        source = next((r for r in self.catalog(user)["items"] if r["source_id"] == source_id), None)
        need(source, "来源不存在或项目未授权", 403)
        payload = {k: clean(data, k, 2000, False) for k in
                   ("version", "cutoff", "formula", "scope", "target", "approval", "actual", "evidence", "rule_version", "lamp")}
        need(payload["version"] and payload["version"] != "待确认", "请填写具体项目版本")
        need(payload["lamp"] in {"", "green", "yellow", "red"}, "灯色不支持")
        if payload["cutoff"]:
            payload["cutoff"] = cutoff_date(payload["cutoff"])
        payload.update({"name": source["name"], "source": source["source"]})
        with self.store.connection(True) as db:
            current = db.execute("SELECT * FROM evidence_records WHERE source_id=? AND project=? AND version=? AND stage=?",
                                 (source_id, source["project"], payload["version"], source["stage"])).fetchone()
            self.revision(data, current)
            # Once submitted, every contributor remains excluded from independent verification.
            if current:
                need(user["role"] == "owner" or current["editor"] == user["id"], "仅负责人或登记人可修改该记录", 403)
            payload["snapshot_sha256"] = self.blob(db, encode(source["snapshot"]).encode())
            rev = (current["revision"] if current else 0) + 1
            args = (source_id, source["project"], payload["version"], source["stage"], encode(payload), source["fingerprint"],
                    "draft", rev, user["id"], now())
            db.execute("INSERT INTO evidence_records(source_id,project,version,stage,payload,fingerprint,state,revision,editor,changed) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,project,version,stage) DO UPDATE SET "
                       "payload=excluded.payload,fingerprint=excluded.fingerprint,state='draft',revision=excluded.revision,"
                       "editor=excluded.editor,submitter=NULL,reviewer=NULL,changed=excluded.changed", args)
            identity = db.execute("SELECT id FROM evidence_records WHERE source_id=? AND project=? AND version=? AND stage=?", args[:4]).fetchone()[0]
            self.store.event(db, user["id"], "evidence.save", str(identity), {"revision": rev, "payload": payload, "fingerprint": source["fingerprint"]})
        return {"id": identity, "revision": rev}

    def evidence_action(self, user, data):
        identity = number(data.get("id"), "记录")
        action = clean(data, "action", 20)
        note = clean(data, "note")
        records = {r["id"]: r for r in self.records(user)["records"]}
        need(identity in records, "记录不存在或项目未授权", 403)
        with self.store.connection(True) as db:
            row = db.execute("SELECT * FROM evidence_records WHERE id=?", (identity,)).fetchone()
            self.revision(data, row)
            need(records[identity]["effective_state"] != "stale", "来源已更新，请重新登记并复核", 409)
            payload = json.loads(row["payload"])
            if action == "submit":
                need(row["state"] in {"draft", "rejected"} and user["id"] == row["editor"], "仅登记人可提交草稿", 403)
                need(all(payload.get(k) and payload[k] != "待确认" for k in
                         ("version", "cutoff", "formula", "scope", "target", "approval", "actual", "evidence", "rule_version")), "口径、目标批准、实绩和证据须齐全")
                state, submitter, verifier = "submitted", user["id"], None
            else:
                reviewer(user)
                need(action in {"verify", "reject"} and row["state"] == "submitted", "当前状态不可复核", 409)
                contributors = {r[0] for r in db.execute("SELECT actor FROM events WHERE action='evidence.save' AND subject=?", (str(identity),))}
                need(user["id"] not in contributors and user["id"] != row["submitter"], "复核人须独立于该记录所有修改及提交人", 403)
                state, submitter, verifier = ("verified" if action == "verify" else "rejected"), row["submitter"], user["id"]
            db.execute("UPDATE evidence_records SET state=?,submitter=?,reviewer=?,revision=revision+1,changed=? WHERE id=?",
                       (state, submitter, verifier, now(), identity))
            self.store.event(db, user["id"], "evidence." + action, str(identity), {"note": note, "revision": row["revision"] + 1})
        return {"id": identity, "state": state}

    def team(self, user):
        with self.store.connection() as db:
            rows = db.execute("SELECT u.id,u.name,u.role,u.enabled,r.projects,r.specialties,r.revision,r.changed "
                              "FROM users u LEFT JOIN responsibilities r ON r.user_id=u.id ORDER BY u.id").fetchall()
        return {"people": [{**dict(r), "projects": json.loads(r["projects"] or "[]"),
                            "specialties": json.loads(r["specialties"] or "[]")} for r in rows]}

    def team_save(self, user, data):
        owner(user)
        note = clean(data, "note")
        projects = data.get("projects")
        specialties = data.get("specialties")
        need(all(isinstance(v, list) and len(v) <= 30 and all(isinstance(s, str) and 0 < len(s.strip()) <= 100 for s in v)
                 for v in (projects, specialties)), "项目和专项须为名称列表")
        with self.store.connection(True) as db:
            target = db.execute("SELECT id FROM users WHERE id=?", (number(data.get("id"), "账号"),)).fetchone()
            need(target, "账号不存在", 404)
            current = db.execute("SELECT * FROM responsibilities WHERE user_id=?", (target[0],)).fetchone()
            self.revision(data, current)
            rev = (current["revision"] if current else 0) + 1
            db.execute("INSERT INTO responsibilities VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                       "projects=excluded.projects,specialties=excluded.specialties,revision=excluded.revision,changed=excluded.changed",
                       (target[0], encode(sorted(set(s.strip() for s in projects))), encode(sorted(set(s.strip() for s in specialties))), rev, now()))
            self.store.event(db, user["id"], "responsibility.save", str(target[0]), {"before": dict(current) if current else None,
                             "projects": projects, "specialties": specialties, "note": note})
        return {"revision": rev}

    def documents(self, user):
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM controlled_documents ORDER BY code").fetchall()
            histories = db.execute("SELECT * FROM controlled_document_history ORDER BY document_id,revision DESC").fetchall()
        visible = lambda r: user["role"] in {"owner", "reviewer"} or r["state"] == "effective"
        documents = []
        for row in rows:
            current = dict(row)
            if not visible(row) and row["state"] != "obsolete":
                prior = next((h for h in histories if h["document_id"] == row["id"] and h["state"] == "effective"), None)
                if prior:
                    current.update(dict(prior))
                    current["id"] = row["id"]
            if visible(current):
                documents.append({**current, "payload": json.loads(current["payload"])})
        return {"documents": documents,
                "history": [{**dict(r), "payload": json.loads(r["payload"])} for r in histories if visible(r)],
                "candidates": [{"key": name, "name": name} for name in DOC_NAMES if (DOC_ROOT / name).is_file()] if user["role"] == "owner" else []}

    def document_save(self, user, data):
        owner(user)
        code = clean(data, "code", 100)
        payload = {k: clean(data, k, 2000, False) for k in
                   ("title", "version", "owner", "approval_evidence", "effective_date", "retention", "source_key", "url", "note")}
        need(payload["title"] and payload["version"] and payload["owner"], "文件名称、版本标识和责任人须填写，可标待确认")
        payload["retention"] = payload["retention"] or "待确认"
        if payload["url"]:
            url = urlsplit(payload["url"])
            need(url.scheme == "https" and url.hostname == "suuntoglobal.feishu.cn" and not url.username, "文件链接须使用公司飞书 HTTPS 地址")
        with self.store.connection(True) as db:
            current = db.execute("SELECT * FROM controlled_documents WHERE code=?", (code,)).fetchone()
            self.revision(data, current)
            if payload["source_key"]:
                need(payload["source_key"] in DOC_NAMES, "来源文件未列入受控候选清单")
                payload["sha256"] = self.blob(db, (DOC_ROOT / payload["source_key"]).read_bytes())
            else:
                payload["sha256"] = ""
            rev = (current["revision"] if current else 0) + 1
            db.execute("INSERT INTO controlled_documents(code,payload,state,revision,editor,changed) VALUES(?,?,'draft',?,?,?) "
                       "ON CONFLICT(code) DO UPDATE SET payload=excluded.payload,state='draft',revision=excluded.revision,editor=excluded.editor,"
                       "submitter=NULL,approver=NULL,changed=excluded.changed", (code, encode(payload), rev, user["id"], now()))
            identity = db.execute("SELECT id FROM controlled_documents WHERE code=?", (code,)).fetchone()[0]
            self.document_history(db, identity, user, "save", payload.get("note", ""))
        return {"id": identity, "revision": rev}

    def document_history(self, db, identity, user, action, note):
        row = db.execute("SELECT * FROM controlled_documents WHERE id=?", (identity,)).fetchone()
        db.execute("INSERT INTO controlled_document_history VALUES(?,?,?,?,?,?)",
                   (identity, row["revision"], row["payload"], row["state"], user["id"], now()))
        self.store.event(db, user["id"], "document." + action, str(identity), {"revision": row["revision"], "note": note})

    def document_action(self, user, data):
        reviewer(user)
        action = clean(data, "action", 20)
        note = clean(data, "note")
        with self.store.connection(True) as db:
            row = db.execute("SELECT * FROM controlled_documents WHERE id=?", (number(data.get("id"), "文件"),)).fetchone()
            need(row, "文件不存在", 404)
            self.revision(data, row)
            payload = json.loads(row["payload"])
            if action == "submit":
                need(user["id"] == row["editor"] and row["state"] == "draft", "仅登记人可提交草稿", 403)
                need(payload["version"] != "待确认" and payload["owner"] != "待确认" and payload["approval_evidence"] and payload["effective_date"] and
                     (payload["sha256"] or payload["url"]), "请补齐版本、责任人、批准凭据、拟生效日期和文件来源")
                cutoff_date(payload["effective_date"])
                state, submitter, approver = "submitted", user["id"], None
            elif action in {"approve", "reject"}:
                need(row["state"] == "submitted", "文件须先提交审批", 409)
                contributors = {r[0] for r in db.execute("SELECT actor FROM events WHERE action='document.save' AND subject=?", (str(row["id"]),))}
                need(user["id"] not in contributors and user["id"] != row["submitter"], "审批人须独立于本文件登记和提交人", 403)
                state, submitter, approver = ("approved" if action == "approve" else "draft"), row["submitter"], user["id"]
            else:
                owner(user)
                need(action in {"publish", "obsolete"}, "文件操作无效")
                if action == "publish":
                    need(row["state"] == "approved" and payload["effective_date"][:10] <= now()[:10], "须通过审批且到达生效日期", 409)
                    state = "effective"
                else:
                    need(row["state"] in {"effective", "approved"}, "当前状态不可作废", 409)
                    state = "obsolete"
                submitter, approver = row["submitter"], row["approver"]
            db.execute("UPDATE controlled_documents SET state=?,submitter=?,approver=?,revision=revision+1,changed=? WHERE id=?",
                       (state, submitter, approver, now(), row["id"]))
            self.document_history(db, row["id"], user, action, note)
        return {"state": state}

    def snapshot(self, user, sha):
        if user["role"] not in {"owner", "reviewer"}:
            effective = self.documents(user)["documents"]
            need(any(row["payload"].get("sha256") == sha for row in effective),
                 "仅可读取当前生效文件的快照", 403)
        with self.store.connection() as db:
            need(db.execute("SELECT 1 FROM governance_blobs WHERE sha256=?", (sha,)).fetchone(), "证据快照不存在", 404)
        path = self.store.sources / sha
        raw = path.read_bytes()
        need(hashlib.sha256(raw).hexdigest() == sha, "快照校验失败", 409)
        return raw
