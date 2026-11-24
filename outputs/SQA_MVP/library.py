"""Read-only indexes for approved local reports and the released quality board."""

import hashlib
import json
import re
import stat
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from time import monotonic

from store import LOCAL_TZ, Problem, need, now

SOURCE_NAMES = {"bug-reports": "Bug 报告", "culture": "质量文化案例期刊", "crashes": "现网崩溃统计",
                "pr-contributions": "PR 检视贡献看板", "pr-density": "PR 缺陷密度",
                "pr-audit-reports": "PR 规范审计报告"}
REPORT_KEYS = {"pr-contributions", "pr-density", "pr-audit-reports"}
ISSUE = re.compile(r"(?:质量案例第|案例期刊第?)([0-9０-９]+)期")
PATTERNS = {
    "bug-reports": re.compile(r"质量周报|Wont.?fix|Reproduce|双端DI|遗留.*(?:问题|Bug)|"
                              r"(?:Bug|缺陷).*(?:报告|分析|统计|趋势|消减|审计)", re.I),
    "culture": re.compile(r"质量案例第|案例期刊"),
    "crashes": re.compile(r"现网崩溃统计|崩溃提示分布"),
}
TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".md": "text/plain; charset=utf-8"}
ALLOWED = {
    "bug-reports": {".pdf", ".md", ".xlsx", ".docx"},
    "culture": {".pdf", ".png", ".jpg", ".jpeg", ".html"},
    "crashes": {".png", ".jpg", ".jpeg", ".xlsx", ".pdf"},
}
DATE = re.compile(r"(?<!\d)(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)")
MAX_FILE = 32 * 1024 * 1024
STAGES = ("ES", "EP", "FEP", "RID", "OTA")
ALIASES = {
    "Race3S": ("Race3S", "Race 3-3S质量看板"),
    "Race3质量看板": ("Race3（归属待确认）", "Race3质量看板"),
    "Vertical3质量看板": ("Vertical3", "Vertical3质量看板"),
    "Vertical 3S 质量看板": ("Vertical3S", "Vertical 3S 质量看板"),
    "FLEX质量看板": ("Flex", "FLEX质量看板"),
    "Chitu NIO质量看板": ("Chitu NIO", "Chitu NIO质量看板"),
    "Ocean 2质量看板": ("Ocean2", "Ocean 2质量看板"),
    "NG3&BES维护项目质量看板": ("NG3&BES维护专项", "NG3&BES维护项目质量看板"),
}


def date_from_name(name):
    for match in reversed(list(DATE.finditer(name))):
        try:
            return date(*map(int, match.groups())).isoformat()
        except ValueError:
            continue
    return ""


def local_time(timestamp):
    return datetime.fromtimestamp(timestamp, LOCAL_TZ).isoformat(timespec="seconds")


def safe_status(path, root):
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if reparse or path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
        raise OSError("source path escaped")
    return info


def embedded(text, name):
    match = re.search(r"\b" + name + r"\s*=\s*", text)
    if not match:
        raise ValueError(f"missing {name}")
    return json.JSONDecoder().raw_decode(text[match.end():])[0]


def source_board(root, overlay):
    board = root / "项目SQA阶段质量实时度量看板.html"
    info = safe_status(board, root)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
        raise ValueError("invalid release board")
    text = board.read_text(encoding="utf-8-sig")
    if board.stat().st_mtime_ns != info.st_mtime_ns:
        raise ValueError("release board changed during read")
    baseline, seed, profiles, actuals, milestones = (
        embedded(text, key) for key in ("baseline", "sourceActuals", "projectProfiles",
                                         "projectSourceActuals", "projectSourceMilestones"))
    if not isinstance(baseline, list) or not baseline or seed["project"] != "Race3S":
        raise ValueError("invalid release board structure")
    seeded = {"lib-" + index: row for index, row in seed["entries"].items()}
    projects = {}
    for index, row in enumerate(baseline):
        origin = row.get("来源项目")
        if origin not in ALIASES:
            continue
        project, profile = ALIASES[origin]
        project_row = projects.setdefault(project, {"name": project, "profile": profiles.get(profile, {}),
                                                    "milestones": milestones.get(project, {}), "rows": []})
        identity = "lib-" + str(index)
        proof = seeded.get(identity, actuals.get(identity, {}))
        project_row["rows"].append({
            "id": identity, "stage": row["阶段"], "name": row["指标"],
            "definition": row.get("定义 / 计算口径", ""), "target": row.get("目标", ""),
            "target_note": row.get("目标属性", ""), "requirement": row.get("判定说明 / 样本要求", ""),
            "source": row.get("来源", ""), "node": row.get("来源节点", row.get("原始阶段节点", "")),
            "owner": row.get("责任角色", ""), "actual": proof.get("actual", ""),
            "source_lamp": proof.get("lamp", "") if proof.get("actual") and proof.get("evidence") else "",
            "evidence": proof.get("evidence", ""), "action": proof.get("action", ""),
            "snapshot": seed["date"] if identity in seeded else "",
        })
    if not projects or set(milestones) - set(projects):
        raise ValueError("project coverage changed")
    return {"name": board.name, "updated": local_time(info.st_mtime),
            "seed_date": seed["date"], "seed_note": seed["version"], "library_count": len(baseline),
            "projects": list(projects.values()), "stage_reviews": overlay or {}}


class LocalLibrary:
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self.lock = Lock()
        self.checked = ""
        self.expires = 0
        self.groups = {}
        self.assets = {}
        self.errors = {}
        self.board = None
        self.board_stamp = None

    def refresh(self, force=False):
        with self.lock:
            if not force and monotonic() < self.expires:
                return
            self.expires = monotonic() + 300
            self.checked = now()
            try:
                config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                self.errors = {key: "本机来源配置无法读取" for key in (*SOURCE_NAMES, "board")}
                return
            for key in SOURCE_NAMES:
                try:
                    if key in REPORT_KEYS:
                        groups, files = self.scan_reports(key, config[key])
                    else:
                        groups, files = self.scan(key, Path(config[key]).resolve(strict=True))
                    self.groups[key], self.assets[key] = groups, files
                    self.errors.pop(key, None)
                except (KeyError, OSError, TypeError, ValueError):
                    self.errors[key] = "来源目录暂不可读"
            try:
                root = Path(config["board"]).resolve(strict=True)
                html = root / "项目SQA阶段质量实时度量看板.html"
                overlay_path = Path(config["stage_reviews"]) if config.get("stage_reviews") else None
                stamp = (str(html), html.stat().st_mtime_ns, overlay_path.stat().st_mtime_ns if overlay_path else 0)
                if self.board is None or stamp != self.board_stamp:
                    overlay = json.loads(overlay_path.read_text(encoding="utf-8-sig")) if overlay_path else None
                    self.board = source_board(root, overlay)
                    self.board_stamp = stamp
                self.errors.pop("board", None)
            except (KeyError, OSError, ValueError, TypeError, AttributeError):
                self.errors["board"] = "现行看板读取或结构校验失败"

    @staticmethod
    def scan(key, root):
        folders = [root]
        if key == "bug-reports":
            folders.extend(root / name for name in ("pdf", "xlsx") if (root / name).exists())
        groups, files = {}, {}
        for folder in folders:
            try:
                safe_status(folder, root)
            except OSError:
                continue
            for path in sorted(folder.iterdir()):
                suffix = path.suffix.lower()
                if suffix not in ALLOWED[key] or not PATTERNS[key].search(path.stem):
                    continue
                try:
                    info = safe_status(path, root)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE:
                    continue
                relative = path.relative_to(root).as_posix()
                identity = hashlib.sha256(f"{key}/{relative}".encode()).hexdigest()
                tagged_date = date_from_name(path.stem)
                updated = local_time(info.st_mtime)
                title = path.stem
                issue_match = ISSUE.search(title) if key == "culture" else None
                issue = int(issue_match[1]) if issue_match else None
                group_key = f"issue-{issue}" if issue is not None else tagged_date if key == "crashes" and tagged_date else title
                display_title = f"质量案例第{issue:02d}期" if issue is not None else "现网崩溃统计 · " + group_key if key == "crashes" and tagged_date else title
                entry = groups.setdefault(group_key, {"title": display_title,
                    "date": tagged_date or updated[:10], "basis": "文件名日期" if tagged_date else "文件更新日",
                    "updated": updated, "files": []})
                if issue is not None:
                    entry["issue"] = issue
                    entry["basis"] = "最近文件更新日"
                entry["updated"] = max(entry["updated"], updated)
                if issue is not None or not tagged_date:
                    entry["date"] = entry["updated"][:10]
                record = {"id": identity, "name": path.name, "format": suffix[1:].upper(),
                          "size": info.st_size, "preview": suffix in TYPES}
                entry["files"].append(record)
                files[identity] = (root, path, info.st_mtime_ns, info.st_size)
        sorted_groups = sorted(groups.values(), key=lambda row: (row.get("issue", 0), row["date"], row["title"]), reverse=True)
        return sorted_groups, files

    @staticmethod
    def scan_reports(key, reports):
        groups, files = [], {}
        if not isinstance(reports, list):
            raise ValueError("report list required")
        for report in reports:
            path = Path(report["path"]).absolute()
            root = path.parent.resolve(strict=True)
            info = safe_status(path, root)
            if path.suffix.lower() not in {".md", ".pdf"} or not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE:
                raise ValueError("invalid report file")
            identity = hashlib.sha256(f"{key}/{path.as_posix()}".encode()).hexdigest()
            updated = local_time(info.st_mtime)
            groups.append({"title": report["title"], "date": report["date"], "basis": report["basis"],
                           "note": report.get("note", ""), "updated": updated,
                           "files": [{"id": identity, "name": path.name, "format": path.suffix[1:].upper(),
                                      "size": info.st_size, "preview": True}]})
            files[identity] = (root, path, info.st_mtime_ns, info.st_size)
        return sorted(groups, key=lambda row: row["date"], reverse=True), files

    def catalog(self, key, force=False):
        need(key in SOURCE_NAMES, "资料类别不存在", 404)
        self.refresh(force)
        with self.lock:
            return {"groups": self.groups.get(key, []), "checked": self.checked, "error": self.errors.get(key, "")}

    def project_board(self, full=False):
        self.refresh()
        with self.lock:
            data = self.board
            result = {"checked": self.checked, "error": self.errors.get("board", ""), "source": None, "stage_reviews": {}}
        if not data:
            return result
        result["source"] = {key: data[key] for key in ("name", "updated", "seed_date", "seed_note", "library_count")}
        result["stage_reviews"] = data["stage_reviews"]
        projects = []
        for project in data["projects"]:
            counts = Counter(row["stage"] for row in project["rows"])
            lamps = Counter(row["source_lamp"] for row in project["rows"])
            projects.append(project if full else {"name": project["name"], "counts": dict(counts),
                                                  "metrics": len(project["rows"]),
                                                  "with_evidence": sum(bool(row["evidence"]) for row in project["rows"]),
                                                  "lamps": dict(lamps)})
        return {**result, "projects": projects}

    def download(self, key, identity, preview):
        need(key in SOURCE_NAMES and re.fullmatch(r"[0-9a-f]{64}", identity), "文件不存在", 404)
        self.refresh()
        with self.lock:
            asset = self.assets.get(key, {}).get(identity)
        need(asset, "文件不存在", 404)
        root, path, stamp, size = asset
        try:
            before = safe_status(path, root)
            need((before.st_mtime_ns, before.st_size) == (stamp, size), "文件已更新，请刷新目录", 409)
            with path.open("rb") as source:
                data = source.read(MAX_FILE + 1)
            after = safe_status(path, root)
            need((after.st_mtime_ns, len(data)) == (stamp, size), "文件已更新，请刷新目录", 409)
        except OSError:
            raise Problem("文件暂不可读取，请刷新目录", 404) from None
        mime = TYPES.get(path.suffix.lower()) if preview else None
        return path.name, data, mime
