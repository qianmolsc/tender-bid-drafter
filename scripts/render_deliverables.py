#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 parsed.json + matched.json 渲染成三份可交付物。

用法:
    python render_deliverables.py <parsed.json> <matched.json> [-o 输出目录]

产出:
    01_评分点清单.md / .csv   逐条评分点，带原文行号，可直接派活
    02_材料匹配表.md          每个评分点对应哪份材料，缺什么
    03_目录初稿.md            四册结构 + 每章对应评分点与拟用材料
    04_自查清单.md            废标红线 / 形式合规 / 时间节点 / 覆盖度
"""

import argparse
import csv
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BANNER = (
    "> ⚠️ 本文件由脚本从招标文件机械抽取生成，**不是投标文件**，也不构成任何合规结论。\n"
    "> 所有分值、条款编号、材料可用性须回原文逐条核对；报价、盖章、签字、密封、资格承诺必须人工确认。\n"
)

FIXED = {
    "商务前置": [
        ("投标函", "投标函须法定代表人签字并加盖公章，报价与开标一览表一致"),
        ("法定代表人身份证明书", "附身份证复印件，加盖公章"),
        ("法定代表人授权委托书", "如由授权代表签署，须附授权书及被授权人身份证"),
        ("商务条款偏离表", "逐条响应付款方式、履约保证金、违约责任、服务期"),
        ("投标人基本情况表", "公司简介、组织架构、人员规模、本地服务机构情况"),
    ],
    "技术固定": [
        ("项目理解与需求分析", "按招标文件 5 类系统、37 个子系统逐一拆解，禁止通用模板"),
        ("运维服务体系与实施计划", "含服务台、巡检、变更、发布、备份、容量管理流程"),
        ("项目过渡与接管方案", "进场 30 日内的接管步骤、风险与回退安排"),
        ("技术条款偏离表", "逐条响应技术需求书，★条款不得负偏离"),
        ("服务承诺函", "7×24 值守、SLA、人员稳定性的书面承诺"),
    ],
    "报价": [
        ("开标一览表", "单独密封，唱标用；总价须与分项报价表合计一致"),
        ("分项报价表", "按人力、备件、差旅、税费分项，单价×数量须可复核"),
        ("报价说明", "报价构成、税率、是否包含第三方软件维保费用"),
    ],
}


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def cell(s):
    return str(s if s is not None else "").replace("|", "／").replace("\n", " ").strip()


def suggest_len(score, category=""):
    if "价格" in category or "报价" in category:
        return "—（报价不写篇幅）"
    if not score:
        return "按条款要求"
    n = int(score * 300)
    return "≥%d 字" % (max(800, (n // 500) * 500))


def fmt_score(s):
    if s is None:
        return "—"
    return ("%g" % s)


def render_scoring(parsed, matched, outdir):
    meta = parsed["meta"]
    items = parsed["scoring"]["items"]
    md = ["# 评分点清单", "", BANNER, ""]
    md.append("**项目**：%s  " % (meta.get("project_name") or "（未识别）"))
    md.append("**编号**：%s  " % (meta.get("project_no") or "—"))
    md.append("**评标方法**：%s，总分 %s 分  " % (meta.get("scoring_method") or "待确认", meta.get("total_score") or "—"))
    md.append("**预算**：%s  " % (meta.get("budget_text") or "—"))
    md.append("**清单合计**：%.1f 分（与声明总分差异须人工核对）" % parsed["scoring"]["sum"])
    md.append("")
    md.append("| 编号 | 评分因素 | 类别 | 分值 | 类型 | 评分标准（摘要） | 建议篇幅 | 原文行 |")
    md.append("|---|---|---|---|---|---|---|---|")
    for i, it in enumerate(items, 1):
        md.append("| S-%02d | %s | %s | %s | %s | %s | %s | L%s |" % (
            i, cell(it["name"]), cell(it["category"]), fmt_score(it["score"]),
            cell(it["type"]), cell(it["criteria"])[:160], suggest_len(it["score"], it["category"]), it["line"]))
    md.append("")
    md.append("## 评分策略提示")
    md.append("")
    is_price = lambda it: ("价格" in (it["category"] or "")) or it["name"] == "投标报价"
    price = sum(it["score"] or 0 for it in items if is_price(it))
    obj = sum(it["score"] or 0 for it in items if it["type"] == "客观" and not is_price(it))
    subj = sum(it["score"] or 0 for it in items if it["type"] in ("主观", "半主观") and not is_price(it))
    md.append("- 客观分（拼材料齐备度）约 %.0f 分：缺一份证书/合同就是硬丢分，先补齐再谈文采。" % obj)
    md.append("- 主观分（拼方案针对性）约 %.0f 分：必须逐系统、逐场景写，套模板只能拿低档。" % subj)
    if price:
        md.append("- 价格分 %.0f 分：由商务按成本模型测算，本清单不提供任何报价建议。" % price)
    md.append("- 带 ★ 的条款不计分但决定生死，见自查清单 A 区。")
    md.append("")
    path = os.path.join(outdir, "01_评分点清单.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    csv_path = os.path.join(outdir, "01_评分点清单.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["编号", "评分因素", "类别", "分值", "类型", "评分标准", "建议篇幅", "原文行", "原文摘录"])
        for i, it in enumerate(items, 1):
            w.writerow(["S-%02d" % i, it["name"], it["category"], it["score"], it["type"],
                        it["criteria"], suggest_len(it["score"]), it["line"], it["evidence"]])
    return path, csv_path


def render_match(matched, outdir):
    st = matched["stats"]
    md = ["# 材料匹配表", "", BANNER, ""]
    md.append("需求 %d 条：**已覆盖 %d / 需改写或部分覆盖 %d / 缺口 %d / 待报价 %d**；"
              "评分点 %.1f 分中：已覆盖 %.1f 分、需改写 %.1f 分、缺口 %.1f 分、报价待定 %.1f 分。"
              % (st["requirements"], st["covered"], st["partial"], st["gap"], st.get("price_pending", 0),
                 st["scoring_total"], st["scoring_covered"], st.get("scoring_partial", 0),
                 st["scoring_gap"], st.get("price_score", 0)))
    md.append("")
    md.append("| 需求 | 评分因素 / 条款 | 分值 | 状态 | 拟用材料 | 相似度 | 处置建议 |")
    md.append("|---|---|---|---|---|---|---|")
    for r in matched["requirements"]:
        m = r["matches"][0] if r["matches"] else None
        mat = "%s %s" % (m["id"], m["name"]) if m else "—"
        sim = "%.2f" % m["sim"] if m else "—"
        md.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            r["id"], cell(r["text"])[:60], fmt_score(r["score"]), r["status"],
            cell(mat)[:70], sim, cell(r["gap_note"])[:80]))
    md.append("")
    md.append("## 缺口与风险明细")
    md.append("")
    risky = [r for r in matched["requirements"] if r["status"] != "已覆盖"]
    if not risky:
        md.append("无。")
    for r in risky:
        md.append("### %s｜%s（%s）" % (r["id"], r["text"][:50], r["status"]))
        md.append("- 原文：`%s`" % cell(r["evidence"])[:200])
        if r["matches"]:
            md.append("- 候选：%s" % "；".join("%s(%s, %.2f)" % (m["name"][:24], m["id"], m["sim"]) for m in r["matches"]))
        if r["gap_note"]:
            md.append("- 处置：%s" % r["gap_note"])
        md.append("")
    md.append("## 材料有效期预警（全库扫描，与是否被引用无关）")
    md.append("")
    alerts = matched.get("expiry_alerts", [])
    if not alerts:
        md.append("无过期或 60 日内到期材料。")
    for a in alerts:
        md.append("- **%s**｜%s %s｜%s" % (
            a["level"], a["id"], a["name"][:34],
            ("已过期 %d 天" % -a["days_left"]) if a["days_left"] < 0 else ("剩 %d 天（%s）" % (a["days_left"], a["valid_until"]))))
        if a["note"]:
            md.append("  - %s" % a["note"])
    md.append("")
    path = os.path.join(outdir, "02_材料匹配表.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    return path


def render_outline(parsed, matched, outdir):
    meta = parsed["meta"]
    reqs = matched["requirements"]
    items = [r for r in reqs if r["id"].startswith("S-")]
    quals = [r for r in reqs if r["id"].startswith("Q-")]

    def rows_for(cats, keywords):
        """按评分类别归册（技术/商务），类别缺失时才退回关键词匹配。"""
        out = []
        for r in items:
            c = r["category"] or ""
            if (c and any(k in c for k in cats)) or (not c and any(k in r["text"] for k in keywords)):
                out.append(r)
        return out

    def short_title(r, n=26):
        t = r["text"].lstrip("#★▲ ").strip()
        t = re.sub(r"^\d+[\d\.]*[、\.]\s*", "", t)
        t = re.split(r"[，；。]", t)[0]
        return t[:n]

    def chapter_of(r):
        """章节名用评分因素/条款短名；材料名一律放“拟用材料”列，两者不要混。"""
        t = r["text"].lstrip("# ").strip()
        t = re.sub(r"^\d+[\d\.]*[、\.]\s*", "", t)
        t = re.sub(r"^[★▲]\s*", "", t)
        t = re.sub(r"^(投标人|供应商)(须|应|必须)", "", t)
        t = re.split(r"[，；。]", t)[0].strip()
        return t[:22] or "（待命名）"

    def mat_of(r):
        if not r["matches"]:
            return "**待补**"
        m = r["matches"][0]
        flag = {"已覆盖": "备", "缺口": "缺", "需换证/过期风险": "过期"}.get(r["status"], "改写")
        return "%s %s（%s）" % (m["id"], m["name"][:26], flag)

    md = ["# 投标文件目录（初稿）", "", BANNER, ""]
    md.append("**项目**：%s　**编号**：%s　**投标人**：%s"
              % (meta.get("project_name") or "—", meta.get("project_no") or "—", matched.get("company") or "—"))
    md.append("")
    md.append("> 页码列为占位，定稿排版后回填；篇幅为下限建议，不是评分依据。")
    md.append("")

    md.append("## 第一册　资格证明文件")
    md.append("")
    md.append("| 序号 | 章节 | 对应条款 | 拟用材料 | 状态 | 页码 |")
    md.append("|---|---|---|---|---|---|")
    for i, r in enumerate(quals, 1):
        md.append("| 1.%d | %s | %s %s | %s | %s | P__ |" % (
            i, cell(chapter_of(r)), r["id"], cell(short_title(r, 40)),
            cell(mat_of(r))[:52], r["status"]))
    md.append("")

    md.append("## 第二册　商务文件")
    md.append("")
    md.append("| 序号 | 章节 | 对应评分点 | 分值 | 要点 / 拟用材料 | 页码 |")
    md.append("|---|---|---|---|---|---|")
    n = 1
    for name, tip in FIXED["商务前置"]:
        md.append("| 2.%d | %s | 形式性内容（不计分但缺则废） | — | %s | P__ |" % (n, name, tip))
        n += 1
    for r in rows_for(["商务", "资信"], ["资质", "业绩", "团队", "财务", "信用", "证书"]):
        md.append("| 2.%d | %s | %s | %s | %s | P__ |" % (
            n, cell(chapter_of(r)), r["id"], fmt_score(r["score"]), cell(mat_of(r))[:60]))
        n += 1
    md.append("")

    md.append("## 第三册　技术文件")
    md.append("")
    md.append("| 序号 | 章节 | 对应评分点 | 分值 | 要点 / 拟用材料 | 建议篇幅 | 页码 |")
    md.append("|---|---|---|---|---|---|---|")
    n = 1
    for name, tip in FIXED["技术固定"][:2]:
        md.append("| 3.%d | %s | 基础章节 | — | %s | ≥2000 字 | P__ |" % (n, name, tip))
        n += 1
    for r in rows_for(["技术", "服务"], ["方案", "团队", "响应", "安全", "经验", "运维", "实施"]):
        md.append("| 3.%d | %s | %s | %s | %s | %s | P__ |" % (
            n, cell(chapter_of(r)), r["id"], fmt_score(r["score"]), cell(mat_of(r))[:60],
            suggest_len(r["score"], r["category"])))
        n += 1
    for name, tip in FIXED["技术固定"][2:]:
        md.append("| 3.%d | %s | 形式性内容 | — | %s | ≥800 字 | P__ |" % (n, name, tip))
        n += 1
    md.append("")

    md.append("## 第四册　报价文件（单独密封）")
    md.append("")
    md.append("| 序号 | 章节 | 要点 | 页码 |")
    md.append("|---|---|---|---|")
    for i, (name, tip) in enumerate(FIXED["报价"], 1):
        md.append("| 4.%d | %s | %s | P__ |" % (i, name, tip))
    md.append("")
    price_req = [r for r in items if "价格" in (r["category"] or "") or "报价" in r["text"][:10]]
    for r in price_req:
        md.append("> 价格分 %s 分（%s）：%s" % (fmt_score(r["score"]), r["id"], cell(r["gap_note"])))
        md.append("")
    md.append("## 写作交接说明")
    md.append("")
    md.append("- 状态为「备」的章节可直接取用材料；「改写」须按评分标准重写，不能原文照搬。")
    md.append("- 状态为「缺」「过期」的，先由商务确认能否补齐，再决定是否投标。")
    md.append("- 技术文件全册不得出现任何报价信息（见废标条款）。")
    path = os.path.join(outdir, "03_目录初稿.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    return path


def render_checklist(parsed, matched, outdir):
    meta = parsed["meta"]
    md = ["# 投标文件自查清单", "", BANNER, ""]
    md.append("项目：%s　编号：%s　投标截止：%s"
              % (meta.get("project_name") or "—", meta.get("project_no") or "—",
                 next((t["time"] for t in parsed.get("timeline", []) if "截止" in t["label"]), "—")))
    md.append("")

    md.append("## A. 废标红线（逐条人工确认，AI 不得代判）")
    md.append("")
    for i, d in enumerate(parsed.get("disqualify_clauses", []), 1):
        md.append("- [ ] A%d %s　`L%s`" % (i, cell(d["text"])[:120], d["line"]))
    if not parsed.get("disqualify_clauses"):
        md.append("- [ ] **未解析到废标条款，必须回原文人工查找**")
    md.append("")

    md.append("## B. ★ 实质性条款响应")
    md.append("")
    for i, s in enumerate([x for x in parsed.get("star_clauses", []) if x["level"] == "★"], 1):
        md.append("- [ ] B%d %s　`L%s`" % (i, cell(s["text"])[:120], s["line"]))
    md.append("")

    md.append("## C. 形式合规（份数、密封、签署、编排）")
    md.append("")
    for i, f in enumerate(parsed.get("format_requirements", []), 1):
        md.append("- [ ] C%d〔%s〕%s　`L%s`" % (i, f["type"], cell(f["text"])[:110], f["line"]))
    md.append("")

    md.append("## D. 时间节点倒排")
    md.append("")
    for i, t in enumerate(parsed.get("timeline", []), 1):
        md.append("- [ ] D%d %s：**%s**　`L%s`" % (i, t["label"], t["time"], t["line"]))
    md.append("")

    md.append("## E. 评分点覆盖与材料齐备")
    md.append("")
    md.append("| 评分点 | 分值 | 状态 | 材料 | 待办 |")
    md.append("|---|---|---|---|---|")
    for r in [x for x in matched["requirements"] if x["id"].startswith("S-")]:
        m = r["matches"][0]["name"][:28] if r["matches"] else "—"
        todo = r["gap_note"] or "取用并加盖公章"
        md.append("| %s %s | %s | %s | %s | %s |" % (
            r["id"], cell(r["text"])[:36], fmt_score(r["score"]), r["status"], cell(m), cell(todo)[:60]))
    md.append("")

    md.append("## F. 必须人工复核的环节")
    md.append("")
    gates = [
        ("报价", "总价是否超预算 %s；开标一览表与分项报价表是否一致；是否唯一报价" % (meta.get("budget_text") or "—")),
        ("签字盖章", "投标函、授权书是否法定代表人签字＋公章；包封封口、骑缝章是否齐"),
        ("密封递交", "正副本份数、U 盘单独密封、报价文件单独密封、送达时间"),
        ("资格与业绩", "证书有效期、业绩金额与时间区间、社保月份是否满足"),
        ("承诺类", "本地服务机构、不分包、保密、无犯罪记录等实质性承诺是否经授权人签批"),
        ("废标项", "A 区逐条打勾，任何一条不能确认即不得递交"),
    ]
    for i, (k, v) in enumerate(gates, 1):
        md.append("- [ ] F%d **%s**：%s" % (i, k, v))
    md.append("")
    md.append("> 递交前 30 分钟，由非编写人再独立走一遍 A、C、F 三区。")
    md.append("")
    md.append("## G. 材料有效期与原件核对")
    md.append("")
    for a in matched.get("expiry_alerts", []):
        md.append("- [ ] G-%s %s｜%s｜有效期至 %s%s" % (
            a["id"], a["level"], a["name"][:34], a["valid_until"],
            "（已过期 %d 天）" % -a["days_left"] if a["days_left"] < 0 else ""))
    if not matched.get("expiry_alerts"):
        md.append("- [ ] 无预警，但仍须核对每份证书原件的有效期与年检页")
    md.append("")
    md.append("> 材料清单只说明“库里有”，不说明“这份能用”：等级、金额、行业、时间区间、在职状态，逐项人工核。")
    path = os.path.join(outdir, "04_自查清单.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("parsed")
    ap.add_argument("matched")
    ap.add_argument("-o", "--outdir", default="bid_output")
    args = ap.parse_args()

    parsed, matched = load(args.parsed), load(args.matched)
    os.makedirs(args.outdir, exist_ok=True)

    paths = []
    paths += list(render_scoring(parsed, matched, args.outdir))
    paths.append(render_match(matched, args.outdir))
    paths.append(render_outline(parsed, matched, args.outdir))
    paths.append(render_checklist(parsed, matched, args.outdir))

    print("交付物已生成：")
    for p in paths:
        print("  - " + p)


if __name__ == "__main__":
    main()
