"""Parse the three approved pilot source formats without changing source files."""

import csv
import io
import json
import re
from collections import Counter, defaultdict

PROJECT = "Race3S"
VERSION = "2.57.64"
STAGE = "RID"
RID_LABEL = "NG RACE 3/3S RELEASE NVWA RID"
TEAMS = {"ESW China NG3 Driver", "ESW China NG3 Framework", "ESW UI Team"}
SEVERITIES = {"Blocking", "Critical", "Major", "Normal"}
EXCLUDED = {"Invalid", "Wont fix", "Duplicate"}
PENDING = {"New", "In Progress", "In Review"}
PR_CATEGORIES = {"New Features", "Bugfixes", "Technical", "Testing"}
MATCH_STATES = {"MATCHED", "AMBIGUOUS", "UNMATCHED"}
ALLOWED_LAMPS = {"green", "yellow", "red", ""}
IMPORT_FILES = {
    "metrics": ("backup",),
    "pr": ("summary", "items", "matches", "audit"),
    "bugs": ("snapshot",),
}


class InvalidImport(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidImport(message)


def read_csv(data, required):
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
    headers = reader.fieldnames or []
    require(len(headers) == len(set(headers)), "CSV 有重复列名")
    require(set(required) <= set(headers), "CSV 缺少列 " + "、".join(sorted(set(required) - set(headers))))
    rows = []
    for row in reader:
        require(None not in row and None not in row.values(), f"CSV 第 {reader.line_num} 行列数不符")
        rows.append({key: value.strip() for key, value in row.items()})
    require(rows, "CSV 没有数据行")
    return rows


def unique(rows, key, group):
    seen = set()
    for row in rows:
        identity = key(row)
        require(identity and identity not in seen, f"{group} 主键缺失或重复")
        seen.add(identity)


def metric_rows(rows, label):
    require(isinstance(rows, list), f"{label} 的指标行格式不正确")
    selected = [row for row in rows if row["stage"] == STAGE]
    require(selected, f"{label} 没有 RID 指标")
    unique(selected, lambda row: row["id"], label)
    require(all(row["name"] and row.get("lamp", "") in ALLOWED_LAMPS for row in selected),
            f"{label} 存在无名称指标或未知灯色")
    return selected


def metrics(files, _cutoff):
    document = json.loads(files["backup"].decode("utf-8-sig"))
    require(document["schema"] == 2, "看板备份需为 Schema 2")
    candidates = [p for p in document["projects"].values() if p["name"] == PROJECT]
    require(len(candidates) == 1, "备份须包含唯一 Race3S 项目")
    project = candidates[0]
    items = metric_rows(project["items"], "项目配置")
    snapshots = []
    for label, snapshot in sorted(project["snapshots"].items()):
        rows = metric_rows(snapshot["rows"], label)
        snapshots.append({"label": label, "source_version": snapshot.get("version", ""),
                          "note": snapshot.get("sourceNote", ""), "rows": rows})
    require(snapshots, "备份缺少历史快照")
    return {"source_project": project["id"], "milestone": project.get("milestones", {}).get(STAGE, {}),
            "items": items, "snapshots": snapshots}


def pr_sources(files):
    return (
        read_csv(files["summary"], ("版本", "发布日期", "PR条目合计", *PR_CATEGORIES)),
        read_csv(files["items"], ("版本", "发布日期", "分类", "分类内序号", "PR条目原文", "TP原文", "原文行号")),
        read_csv(files["matches"], ("release_version", "pr_type", "release_title", "match_status", "matched_pr_id")),
        read_csv(files["audit"], ("release_version", "pr_id", "pr_type", "release_title", "rule_id",
                                 "rule_name", "result", "evidence", "issue")),
    )


def prs(files, _cutoff, sources=None):
    version = _cutoff or VERSION
    require(re.fullmatch(r"\d{1,6}(?:\.\d{1,6}){2,3}", version), "PR 版本号格式无效")
    summary, items, matches, audit = sources if sources else pr_sources(files)
    summaries = [row for row in summary if row["版本"] == version]
    require(len(summaries) == 1, f"需要唯一的 {version} 汇总")
    headline = summaries[0]
    picked = [row for row in items if row["版本"] == version]
    require(picked and len(picked) == int(headline["PR条目合计"]), "PR 条目总数与汇总不同")
    categories = Counter(row["分类"] for row in picked)
    require(set(categories) <= PR_CATEGORIES and all(categories[key] == int(headline[key]) for key in PR_CATEGORIES),
            "PR 分类计数与汇总不同")
    require(all(row["发布日期"] == headline["发布日期"] for row in picked), "PR 发布日期与汇总不同")
    unique(picked, lambda row: (row["分类"], row["分类内序号"]), "Release Note")
    relevant = [row for row in matches if row["release_version"] == version]
    require(len(relevant) == len(picked), "匹配清单和 Release Note 条目数不同")
    by_title = {}
    def match_key(row):
        return row["pr_type"], row["release_title"]
    counts = Counter(match_key(row) for row in relevant)
    item_keys = [(item["分类"], item["PR条目原文"].removeprefix("* ").strip()) for item in picked]
    require(counts == Counter(item_keys), "PR 明细与匹配清单的标题或重复次数无法对应")
    for row in relevant:
        key = match_key(row)
        require(key not in by_title or by_title[key] == row, "同名发布条目的匹配相互冲突")
        by_title[key] = row
    rules = [row for row in audit if row["release_version"] == version]
    by_entry = defaultdict(dict)
    rule_counts = Counter()
    for row in rules:
        key = (*match_key(row), row["pr_id"])
        rule_id = row["rule_id"]
        require(rule_id and (rule_id not in by_entry[key] or by_entry[key][rule_id] == row), "同一条目存在冲突的规则结论")
        by_entry[key][rule_id] = row
        rule_counts[(*key, rule_id)] += 1
    expected_keys = {(*key, by_title[key]["matched_pr_id"]) for key in by_title}
    require(set(by_entry) <= expected_keys, "PR 规则明细包含未关联的发布条目")
    require(all(count == counts[key[:2]] for key, count in rule_counts.items()), "PR 规则重复次数与发布条目不符")
    result = []
    for item, key in zip(picked, item_keys):
        match = by_title[key]
        state = match["match_status"]
        require(state in MATCH_STATES, "未知 PR 匹配状态")
        pr_id = match["matched_pr_id"] if state == "MATCHED" else ""
        require(state != "MATCHED" or pr_id.isdecimal(), "已匹配 PR 缺少有效编号")
        require(state == "MATCHED" or not match["matched_pr_id"], "未匹配发布条目含已确认的 PR 编号")
        detail = list(by_entry.get((*key, pr_id), {}).values())
        result.append({"key": item["分类"] + ":" + item["分类内序号"], "item": item,
                       "match": match, "rules": detail, "pr_id": pr_id})
    return {"summary": headline, "entries": result, "audit_count": len(rules)}


def bugs(files, cutoff):
    rows = read_csv(files["snapshot"], ("ID", "Name", "Teams", "Severity", "Entity State", "Release"))
    selected = [row for row in rows if row["Release"] == RID_LABEL]
    require(selected, "文件中没有 Race 3 / 3S RID 阶段标签")
    unique(selected, lambda row: row["ID"], "RID Bug 快照")
    require(all(row["ID"].isdecimal() and row["Entity State"] for row in selected), "Bug ID 或状态无效")
    standard = [row for row in selected if row["Teams"] in TEAMS and row["Severity"] in SEVERITIES]
    effective = [row for row in standard if row["Entity State"] not in EXCLUDED]
    states = Counter(row["Entity State"] for row in effective)
    pending = [row for row in effective if row["Entity State"] in PENDING]
    severity = Counter(row["Severity"] for row in pending)
    stats = {"raw_total": len(rows), "rid_rows": len(selected), "standard": len(standard),
             "effective": len(effective), "pending": len(pending), "testing": states["In Testing"],
             "reproduce": states["Reproduce"], "verified_fixed": states["Verified"] + states["Fixed"],
             "blocking": severity["Blocking"], "critical": severity["Critical"],
             "closure": round((1 - len(pending) / len(effective)) * 100, 2) if effective else None}
    return {"cutoff": cutoff, "scope": RID_LABEL, "stats": stats, "rows": effective,
            "rid_ids": sorted(row["ID"] for row in selected)}


def parse(kind, files, cutoff, prepared_pr=None):
    try:
        require(set(files) == set(IMPORT_FILES[kind]), "来源文件组合不完整")
        if kind == "pr":
            return prs(files, cutoff, prepared_pr)
        return {"metrics": metrics, "pr": prs, "bugs": bugs}[kind](files, cutoff)
    except InvalidImport:
        raise
    except (KeyError, ValueError, TypeError, AttributeError, csv.Error) as exc:
        raise InvalidImport("文件格式或字段值无效，核对原始导出文件后重试") from exc
