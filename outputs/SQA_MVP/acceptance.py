"""Run P1 acceptance on the six authorized source files in an isolated directory."""

import argparse
import base64
import json
import secrets
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import PilotServer
from backup import backup, restore
from store import Store


class Browser:
    def __init__(self, origin):
        self.origin = origin
        self.cookie = ""
        self.csrf = ""

    def request(self, path, data=None, status=200, overrides=None):
        headers = {"Cookie": self.cookie}
        body = None
        if data is not None:
            headers.update({"Content-Type": "application/json", "X-SQA-Request": "pilot", "X-SQA-CSRF": self.csrf,
                            "Origin": self.origin})
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers.update(overrides or {})
        try:
            response = urlopen(Request(self.origin + path, data=body, headers=headers), timeout=25)
        except HTTPError as failure:
            response = failure
        with response:
            raw = response.read()
            assert response.code == status, (path, response.code, raw[:300])
            cookie = response.headers.get("Set-Cookie")
            if cookie:
                self.cookie = cookie.split(";", 1)[0]
            result = json.loads(raw) if response.headers.get_content_type() == "application/json" else raw
        if isinstance(result, dict) and result.get("csrf"):
            self.csrf = result["csrf"]
        return result


def check(server, store, destination):
    owner, xiao, third, other = [Browser(server.origin) for _ in range(4)]
    password = secrets.token_urlsafe(24)
    assert b"app.js" in owner.request("/")
    assert b"chart-row" in owner.request("/style.css")
    assert owner.request("/api/session")["setup"]
    owner.request("/api/dashboard", status=401)
    owner.request("/api/setup", {"name": "验收负责人", "password": password},
                  status=403, overrides={"Origin": "https://invalid.example"})
    owner.request("/api/session", status=403, overrides={"Host":"local.invalid"})
    founder = owner.request("/api/setup", {"name": "验收负责人", "password": password})["user"]
    assert founder["role"] == "owner"
    accounts = {}
    for name, role in (("验收审核人", "reviewer"), ("验收验证人", "member"), ("验收普通成员", "member")):
        accounts[name] = owner.request("/api/account.create", {"name": name, "password": password, "role": role})["id"]
    owner.request("/api/samples.stage", {}, status=403, overrides={"X-SQA-CSRF": "not-valid"})
    staged = owner.request("/api/samples.stage", {})
    assert staged["metrics"]["summary"] == {"配置记录":34, "2026-10-08":24, "2026-10-31":34}
    assert staged["pr"]["summary"] == {"发布条目":3, "唯一匹配条目":3, "逐规则明细":24}
    pre_import = list((store.root / "backups").glob("sqa-pre-import-*.zip"))
    assert len(pre_import) == 1
    from zipfile import ZipFile
    with ZipFile(pre_import[0]) as archive:
        assert set(archive.namelist()) == {"pilot.sqlite3", "backup-manifest.json"}
    assert owner.request("/api/dashboard")["metric_count"] is None
    repeated = owner.request("/api/samples.stage", {})
    assert all(repeated[k]["duplicate"] and repeated[k]["id"] == v["id"] for k,v in staged.items())
    for item in staged.values():
        owner.request("/api/publish", {"id": item["id"]})
    before = owner.request("/api/dashboard")
    assert before["metric_count"] == 34 and before["snapshot_count"] == 2 and before["filled"] == 0
    assert before["pr_count"] == 3 and before["pr_rules"] == 24
    bugs = before["bugs"]
    actual = {k: bugs[k] for k in ("effective", "pending", "testing", "reproduce", "verified_fixed", "closure")}
    assert list(actual.values()) == [123,13,20,16,72,89.43]
    assert len(list((store.root / "backups").glob("sqa-pre-import-*.zip"))) == 1
    bad = base64.b64encode(b"ID,Name\n1,test\n").decode()
    owner.request("/api/import", {"kind":"bugs","cutoff":"2026-10-16",
                 "files":{"snapshot":{"name":"bad.csv","base64":bad}}}, status=400)
    assert owner.request("/api/dashboard") == before
    all_batches = owner.request("/api/batches")
    assert any(row["status"] == "failed" for row in all_batches)
    raw = Path(server.samples["bugs"]["snapshot"]).read_bytes()
    assert owner.request(f"/api/source/{staged['bugs']['id']}/snapshot") == raw
    replacement = owner.request("/api/import", {"kind":"bugs","cutoff":"2026-10-17",
               "files":{"snapshot":{"name":"isolated-rollback.csv","base64":base64.b64encode(raw).decode()}}})
    owner.request("/api/publish", {"id": replacement["id"]})
    assert owner.request("/api/dashboard")["bug_cutoff"] == "2026-10-17"
    owner.request("/api/withdraw", {"id": replacement["id"],"reason":"验收演练"})
    assert owner.request("/api/dashboard") == before
    metrics = owner.request("/api/metrics")
    metric = metrics["payload"]["items"][0]
    fields = {"id": metric["id"],"target": metric["target"] or "100%","actual":"100%","lamp":"green"}
    owner.request("/api/metric.save", fields, status=400)
    assert owner.request("/api/dashboard")["filled"] == 0
    owner.request("/api/metric.save", {**fields, "lamp":"", "evidence":"演练：未取正式凭据"})
    assert owner.request("/api/metrics")["reviews"][metric["id"]]["lamp"] == ""
    pr = owner.request("/api/pr")
    entry = pr["payload"]["entries"][0]
    draft = {"batch": pr["batch"], "key": entry["key"], "rule": entry["rules"][0]["rule_id"],
             "result":"待补证","evidence":"演练中，待核验本版本证据"}
    owner.request("/api/pr.review", draft)
    bug = owner.request("/api/bugs")
    owner.request("/api/bugs.review", {"batch":bug["batch"],"key":bug["payload"]["rows"][0]["ID"],
                  "result":"继续跟踪","evidence":"历史阶段范围演练"})
    tid = owner.request("/api/task.create", {"kind":"pr","batch":pr["batch"],"source_key":entry["key"],
        "title":"验收任务","detail":"查看独立验证记录","due":"2026-12-01","assignee":founder["id"],
        "verifier":accounts["验收审核人"]})["id"]
    act = lambda action, **extra: {"id":tid,"action":action,"note":"验收记录",**extra}
    owner.request("/api/task.action", act("close"), status=409)
    owner.request("/api/task.action", act("submit",contributors=[accounts["验收审核人"]]))
    owner.request("/api/task.action", act("verify"), status=403)
    time.sleep(.75)
    xiao.request("/api/login", {"name":"验收审核人","password":password})
    xiao.request("/api/task.action", act("verify"), status=403)
    owner.request("/api/task.action", act("assign_verifier",verifier=accounts["验收审核人"]), status=400)
    owner.request("/api/task.action", act("assign_verifier",verifier=accounts["验收验证人"]))
    time.sleep(.75)
    third.request("/api/login", {"name":"验收验证人","password":password})
    assert len(third.request("/api/tasks")) == 1
    third.request("/api/pr", status=403)
    third.request("/api/source/1/backup", status=403)
    third.request("/api/task.action", act("verify"))
    xiao.request("/api/task.action", act("close"), status=403)
    owner.request("/api/task.action", act("close"))
    assert owner.request("/api/tasks")[0]["state"] == "closed"
    time.sleep(.75)
    other.request("/api/login", {"name":"验收普通成员","password":password})
    assert other.request("/api/tasks") == []
    other.request("/api/task.action", act("submit"), status=403)
    owner.request("/api/account.status", {"id":accounts["验收普通成员"],"enabled":False})
    owner.request("/api/account.status", {"id":accounts["验收普通成员"],"enabled":True})
    other.request("/api/dashboard", status=401)
    archive = backup(store, "manual")
    recovered = Store(restore(archive, destination))
    assert recovered.login({"name":"验收负责人","password":password})["id"] == founder["id"]
    assert all(owner.request("/api/dashboard")[key] == value for key, value in recovered.dashboard(founder).items())
    assert recovered.source_download(founder, staged["bugs"]["id"], "snapshot")[1] == raw
    assert recovered.tasks(founder)[0]["state"] == "closed"
    print("PASS: metrics 34 / snapshots 24,34; PR 3 / rules 24; RID bugs 123 / 13 / 20 / 89.43%")
    print("PASS: staging, idempotence, rollback, invalid input isolation, evidence-gated lamp")
    print("PASS: independent verification, contributor conflict, owner-only close, session revocation")
    print("PASS: local backup restore, dashboard, evidence, task and credentials")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-config", required=True)
    parser.add_argument("--scratch", required=True)
    args = parser.parse_args()
    samples = json.loads(Path(args.sample_config).read_text(encoding="utf-8-sig"))
    Path(args.scratch).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="acceptance-", dir=args.scratch) as scratch:
        root = Path(scratch)
        store = Store(root / "live")
        with PilotServer(0, store, samples) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                check(server, store, root / "recovered")
            finally:
                server.shutdown()
                worker.join()


if __name__ == "__main__":
    main()
