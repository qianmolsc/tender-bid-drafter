#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评分点 / 资格条款 ↔ 公司材料库 匹配，输出可核对的 matched.json。

用法:
    python material_match.py <parsed.json> <material_library.json> [-o matched.json]

匹配是"候选推荐"而非"结论"：脚本只按字面相似度给出 top-3 候选和缺口标记，
真实能不能用（资质等级、业绩金额、人员在职、证书有效期）必须人工确认。
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PUNCT = re.compile(r"[\s，。、；：？！（）()【】\[\]《》<>\"'“”‘’,.:;/\\|—\-_=+*#%￥$&~`]+")
STRONG = 0.50
WEAK = 0.28


def clean(s):
    return PUNCT.sub("", str(s or ""))


def grams(s, n=2):
    s = clean(s)
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def dice(a, b):
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


def parse_date(s):
    if not s:
        return None
    m = re.search(r"(\d{4})\s*[-/年\.]\s*(\d{1,2})\s*[-/月\.]\s*(\d{1,2})", str(s))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_requirements(parsed):
    reqs, seen_lines = [], set()

    for i, it in enumerate(parsed.get("scoring", {}).get("items", []), 1):
        reqs.append({
            "id": "S-%02d" % i,
            "kind": "评分点",
            "category": it.get("category", ""),
            "text": it.get("name", ""),
            "detail": it.get("criteria", ""),
            "score": it.get("score"),
            "type": it.get("type", ""),
            "line": it.get("line"),
            "evidence": it.get("evidence", ""),
        })

    for st in parsed.get("star_clauses", []):
        if st["line"] in seen_lines:
            continue
        seen_lines.add(st["line"])
        reqs.append({
            "id": "Q-%02d" % (sum(1 for r in reqs if r["id"].startswith("Q-")) + 1),
            "kind": "★实质性条款" if st["level"] == "★" else "▲重要条款",
            "category": "资格/实质性响应",
            "text": st["text"][:80],
            "detail": st["text"],
            "score": None,
            "type": "否决项" if st["level"] == "★" else "评分项",
            "line": st["line"],
            "evidence": st["evidence"],
        })

    for q in parsed.get("qualification", []):
        if q["line"] in seen_lines:
            continue
        seen_lines.add(q["line"])
        reqs.append({
            "id": "Q-%02d" % (sum(1 for r in reqs if r["id"].startswith("Q-")) + 1),
            "kind": "资格条件",
            "category": "资格",
            "text": q["text"][:80],
            "detail": q["text"],
            "score": None,
            "type": "准入条件",
            "line": q["line"],
            "evidence": q["text"],
        })

    return reqs


def score_item(req, item):
    req_text = req["text"] + " " + req["detail"]
    item_text = " ".join([
        item.get("name", ""),
        " ".join(item.get("tags", [])),
        " ".join(item.get("covers", [])),
        item.get("note", ""),
    ])
    sim = dice(grams(req_text), grams(item_text))

    bonus = 0.0
    hits = []
    for kw in item.get("covers", []):
        k = clean(kw)
        if len(k) >= 2 and k in clean(req_text):
            bonus += 0.12
            hits.append(kw)
    sim = min(1.0, sim + min(bonus, 0.36))

    # 类型匹配加权：资质类需求命中资质类材料更可信
    if req["kind"].startswith("★") or req["kind"] == "资格条件":
        if item.get("type") in ("资质", "信用", "财务"):
            sim = min(1.0, sim + 0.05)

    return round(sim, 3), hits


def count_subpoints(criteria):
    """估算一条评分标准里含几个独立得分点。

    分档给分（12—15 分 / 8—11 分）算 1 个点；按“；”拆开且各自带“得X分/提供/具备”的，
    视为多个独立得分点（典型：四张证书各 2 分）。
    """
    bands = re.findall(r"\d+(?:\.\d+)?\s*[-—~至]\s*\d+(?:\.\d+)?\s*分", criteria)
    if len(bands) >= 2:
        return 1
    segs = [s for s in re.split(r"[；;]", criteria) if re.search(r"得\s*\d|提供|具备|附|承诺", s)]
    return max(1, len(segs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("parsed")
    ap.add_argument("library")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    parsed = load(args.parsed)
    lib = load(args.library)
    items = lib.get("items", [])
    today = date.today()

    reqs = build_requirements(parsed)
    results = []

    for req in reqs:
        scored = []
        for item in items:
            sim, hits = score_item(req, item)
            if sim <= 0:
                continue
            expired = False
            d = parse_date(item.get("valid_until"))
            if d and d < today:
                expired = True
            scored.append({
                "id": item["id"],
                "name": item["name"],
                "type": item.get("type", ""),
                "sim": sim,
                "hit_keywords": hits[:4],
                "library_status": item.get("status", "available"),
                "valid_until": item.get("valid_until", ""),
                "expired": expired,
                "confidential": item.get("confidential", False),
                "needs_human": item.get("needs_human", False),
                "note": item.get("note", ""),
                "file": item.get("file", ""),
            })
        scored.sort(key=lambda x: -x["sim"])
        top = [s for s in scored[:4] if s["sim"] >= WEAK]

        is_price = ("价格" in req["category"]) or ("报价" in req["text"][:12])
        if is_price:
            status = "待报价（人工决策）"
            gap = "价格分不由材料库支撑，须按成本模型测算后由商务填写，AI 不得代填"
        elif not top:
            status = "缺口"
            gap = "材料库中未找到可用材料，需新编写或向业务部门索取"
        else:
            best = top[0]
            usable = [m for m in top if m["library_status"] == "available" and not m["expired"]]
            sub = count_subpoints(req["detail"] or "")
            if best["library_status"] == "missing":
                if usable:
                    status = "部分覆盖"
                    gap = "主材料缺失（%s）；已有「%s」可用，须确认其能否覆盖全部得分点" % (
                        best["name"][:24], usable[0]["name"][:24])
                else:
                    status = "缺口"
                    gap = "库中登记为缺失：%s" % (best["note"] or best["name"])
            elif best["expired"]:
                status = "需换证/过期风险"
                gap = "证书已过有效期（%s），须取得新证后才可使用" % best["valid_until"]
            elif best["library_status"] == "partial":
                status = "需改写/部分覆盖"
                gap = best["note"] or "库中仅有通用或部分材料，须针对性改写"
            elif sub >= 2 and len(usable) < sub:
                status = "部分覆盖"
                gap = "该条含约 %d 个独立得分点，现有材料只够 %d 个，须逐点核对补齐" % (sub, len(usable))
            elif best["sim"] >= STRONG:
                status = "已覆盖"
                gap = ""
            else:
                status = "需改写/部分覆盖"
                gap = "仅有相近材料，须按本章节要求改写或补充针对性内容"

        results.append(dict(req, matches=top, status=status, gap_note=gap))

    covered = sum(1 for r in results if r["status"] == "已覆盖")
    partial = sum(1 for r in results if r["status"].startswith("需改写") or r["status"] == "部分覆盖")
    gaps = sum(1 for r in results if r["status"] == "缺口")
    risk = sum(1 for r in results if r["status"] == "需换证/过期风险")
    price = sum(1 for r in results if r["status"].startswith("待报价"))

    scoring_reqs = [r for r in results if r["id"].startswith("S-")]
    price_score = sum(r["score"] or 0 for r in scoring_reqs if r["status"].startswith("待报价"))
    scoring_total = sum(r["score"] or 0 for r in scoring_reqs)
    covered_score = sum(r["score"] or 0 for r in scoring_reqs if r["status"] == "已覆盖")
    partial_score = sum(r["score"] or 0 for r in scoring_reqs if r["status"] in ("需改写/部分覆盖", "部分覆盖"))
    gap_score = sum(r["score"] or 0 for r in scoring_reqs
                    if r["status"] in ("缺口", "需换证/过期风险"))

    # 全库有效期扫描：过期或 60 日内到期，无论是否被匹配到都要亮出来
    expiry_alerts = []
    for it in items:
        d = parse_date(it.get("valid_until"))
        if not d:
            continue
        days = (d - today).days
        if days <= 60:
            expiry_alerts.append({
                "id": it["id"],
                "name": it["name"],
                "valid_until": it["valid_until"],
                "days_left": days,
                "level": "已过期" if days < 0 else "即将到期",
                "note": it.get("note", ""),
            })
    expiry_alerts.sort(key=lambda x: x["days_left"])

    used = {}
    for r in results:
        for m in r["matches"]:
            used.setdefault(m["id"], []).append(r["id"])

    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "company": lib.get("company", ""),
        "requirements": results,
        "material_usage": [
            {"id": it["id"], "name": it["name"], "used_by": used.get(it["id"], [])}
            for it in items
        ],
        "expiry_alerts": expiry_alerts,
        "unused_materials": [it["id"] for it in items if it["id"] not in used],
        "stats": {
            "requirements": len(results),
            "covered": covered,
            "partial": partial,
            "gap": gaps,
            "expiry_risk": risk,
            "price_pending": price,
            "scoring_total": scoring_total,
            "scoring_covered": covered_score,
            "scoring_partial": partial_score,
            "scoring_gap": gap_score,
            "price_score": price_score,
        },
        "disclaimer": "匹配结果为字面相似度候选，不构成任何资格或得分结论，全部须人工复核。",
    }

    path = args.out or os.path.splitext(args.parsed)[0] + ".matched.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    st = out["stats"]
    print("匹配完成 -> %s" % path)
    print("  需求 %d 条：已覆盖 %d / 需改写或部分覆盖 %d / 缺口 %d / 过期风险 %d / 待报价 %d"
          % (st["requirements"], st["covered"], st["partial"], st["gap"], st["expiry_risk"], st["price_pending"]))
    print("  评分点 %.1f 分：已覆盖 %.1f 分 / 需改写或部分覆盖 %.1f 分 / 缺口 %.1f 分 / 待报价 %.1f 分"
          % (st["scoring_total"], st["scoring_covered"], st["scoring_partial"],
             st["scoring_gap"], st["price_score"]))
    if expiry_alerts:
        print("  有效期预警 %d 条：" % len(expiry_alerts))
        for e in expiry_alerts:
            print("    - %s %s（%s，%s）" % (e["level"], e["name"][:30], e["valid_until"],
                                            "已过期 %d 天" % -e["days_left"] if e["days_left"] < 0 else "剩 %d 天" % e["days_left"]))
    if out["unused_materials"]:
        print("  未被引用的材料：%s" % ", ".join(out["unused_materials"]))


if __name__ == "__main__":
    main()
