"""Verify the approved accounts and API permissions through a supplied pilot origin."""

import argparse
from datetime import datetime, timezone, timedelta
import getpass
import json
from pathlib import Path
import secrets
import socket
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROSTER = [
    ("曾建芬", "owner"), ("肖强", "reviewer"), ("薛小玉", "member"),
    ("杜楠楠", "member"), ("陈天忍", "member"), ("夏春燕", "member"),
    ("谢如菊", "member"), ("蒋青霞", "member"), ("王程", "member"),
    ("段郁敏", "member"),
]


class Session:
    def __init__(self, origin):
        self.origin = origin
        self.opener = build_opener(ProxyHandler({}))
        self.cookie = ""
        self.csrf = ""

    def request(self, path, data=None):
        headers = {"Cookie": self.cookie}
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Origin": self.origin,
                            "X-SQA-Request": "pilot", "X-SQA-CSRF": self.csrf})
        try:
            response = self.opener.open(Request(self.origin + path, data=body, headers=headers), timeout=20)
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            payload = json.load(response)
            cookie = response.headers.get("Set-Cookie")
            if cookie:
                self.cookie = cookie.split(";", 1)[0]
        if isinstance(payload, dict) and payload.get("csrf"):
            self.csrf = payload["csrf"]
        return status, payload


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def verify_account(origin, password, name, role):
    session = Session(origin)
    status, logged_in = session.request("/api/login", {"name": name, "password": password})
    if status == 429:
        time.sleep(1)
        status, logged_in = session.request("/api/login", {"name": name, "password": password})
    check(status == 200, f"登录返回 {status}")
    check(logged_in["user"]["name"] == name and logged_in["user"]["role"] == role, "姓名或角色不匹配")
    status, context = session.request("/api/session")
    governance = context.get("governance", False)
    checks = {}
    for path in ("dashboard", "people", "tasks"):
        status, _ = session.request("/api/" + path)
        checks[path] = status
        check(status == 200, f"{path} 返回 {status}")
    for path in ("accounts", "audit", "metrics", "pr", "bugs", "library/culture", "release-board"):
        allowed = role == "owner" or (role == "reviewer" and path in ("accounts", "metrics", "pr", "bugs"))
        allowed = allowed or (governance and path == "release-board")
        expected = 200 if allowed else 403
        status, payload = session.request("/api/" + path)
        checks[path] = status
        check(status == expected, f"{path} 返回 {status}，期望 {expected}")
        if role == "owner" and path == "accounts":
            actual = {(row["name"], row["role"]) for row in payload if row["enabled"]}
            check(actual == set(ROSTER), "启用账号与确认的十人名单不一致")
    if governance:
        for path in ("governance/team", "governance/catalog", "governance/records", "governance/documents"):
            status, payload = session.request("/api/" + path)
            check(status == 200, f"{path} 返回 {status}")
            checks[path] = status
            if path == "governance/catalog" and role == "member":
                projects = set(context.get("projects", []))
                aliases = {"Race3（归属待确认）": "Race3", "Chitu NIO": "Chitu"}
                check(all(aliases.get(row["project"], row["project"]) in projects for row in payload["items"]), "指标项目隔离失效")
        checks["project_scope"] = context.get("projects", [])
    status, _ = session.request("/api/logout", {})
    check(status == 200, "退出失败")
    status, _ = session.request("/api/dashboard")
    check(status == 401, "退出后会话仍可访问看板")
    checks["logout_revocation"] = "passed"
    return checks


def verify_password_rule(origin, password):
    session = Session(origin)
    status, _ = session.request("/api/login", {"name": "曾建芬", "password": password})
    check(status == 200, "密码规则检查登录失败")
    checks = {}
    try:
        for length in (10, 11):
            # An invalid role prevents the probe from creating an account.
            status, payload = session.request("/api/account.create", {
                "name": "密码长度验收探测", "password": secrets.token_hex(8)[:length], "role": "invalid"})
            expected = "密码至少 11 个字符" if length == 10 else "角色无效"
            checks[str(length)] = {"passed": status == 400 and payload.get("error") == expected,
                                   "message": payload.get("error", str(status))}
    finally:
        session.request("/api/logout", {})
    return {"passed": all(row["passed"] for row in checks.values()), "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--password-stdin", action="store_true", help="Read a JSON password from stdin")
    args = parser.parse_args()
    parsed = urlsplit(args.origin)
    check(parsed.scheme in ("http", "https") and parsed.hostname and parsed.path in ("", "/")
          and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
          "访问地址格式无效")
    origin = args.origin.rstrip("/")
    password = json.load(sys.stdin)["password"] if args.password_stdin else getpass.getpass("Initial password: ")
    result = {"checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "origin": origin, "verification_host": socket.gethostname(), "accounts": []}
    for name, role in ROSTER:
        row = {"name": name, "role": role}
        try:
            row["checks"] = verify_account(origin, password, name, role)
            row["passed"] = True
        except Exception as error:
            row.update(passed=False, error=str(error))
        result["accounts"].append(row)
        time.sleep(0.9)
    try:
        result["password_rule"] = verify_password_rule(origin, password)
    except Exception as error:
        result["password_rule"] = {"passed": False, "error": str(error)}
    result["passed"] = sum(row["passed"] for row in result["accounts"])
    result["total"] = len(ROSTER)
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"origin": origin, "passed": result["passed"], "total": result["total"],
                      "password_rule": result["password_rule"]["passed"], "report": str(destination)}, ensure_ascii=False))
    return 0 if result["passed"] == result["total"] and result["password_rule"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
