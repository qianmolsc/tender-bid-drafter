#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""招标文件结构化解析：把一份非标招标文件拆成可逐条核对的 JSON。

用法:
    python tender_parse.py <招标文件> [-o parsed.json]

支持 .md / .txt / .docx（.docx 直接解 XML，不依赖 python-docx）。
.pdf 不解析，请先用 PDF 工具转成文本再喂进来。

设计原则:
    - 只做机械抽取，不做语义判断；每一条都带 line 与 evidence，方便人工回原文核对。
    - 抽不到的东西写进 warnings，绝不编。
"""

import argparse
import html
import json
import os
import re
import sys
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$")
CN_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百]+[章节部分]\s*")
CN_ITEM_RE = re.compile(r"^[一二三四五六七八九十]+、")
NUM_ITEM_RE = re.compile(r"^\s*(\d+)[\.、\)]\s*")

FORMAT_BUCKETS = [
    ("密封与签署", ["密封", "包封", "封口", "骑缝", "签字", "盖章", "公章", "启封"]),
    ("份数", ["正本", "副本", "份数", "一正", "几份"]),
    ("编排与装订", ["页码", "目录", "装订", "分册", "胶装", "连续编排"]),
    ("电子版", ["U盘", "电子版", "电子文件", "PDF"]),
    ("报价文件", ["报价文件", "开标一览表", "唱标", "选择性报价"]),
]

DISQ_RE = re.compile(r"废标|无效投标|无效标|否决其投标|作无效|投标无效|视为无效")
# 实质性条款的标记花样极多：★▲ 是国标写法，※ △ ＊ 和行首 * 在地方标书里大量出现
STAR_RE = re.compile(r"[★▲※△＊]")
LINE_STAR_RE = re.compile(r"^\s*\*(?!\*)\s*(?=\S)")
# 没有任何符号、但属于强制性表述的条款（不能自动当★，只做提醒）
MANDATORY_RE = re.compile(r"(投标人|供应商|乙方|中标人)(须|应|必须|应当)")


def docx_xml_to_text(xml):
    """docx 转文本：先按单元格/行切，再处理段落。

    Word 表格的每个单元格里必然嵌着 <w:p>，如果先把 </w:p> 换成换行，
    表格会被拆成一行一个单元格，评分表就再也解析不出来了——这是这条链路最容易踩的坑。
    """
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = xml.replace("</w:tc>", "\x01").replace("</w:tr>", "\x02").replace("</w:p>", "\x03")
    xml = re.sub(r"<w:br[^>]*/>", "\x03", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    xml = html.unescape(xml)

    out = []
    for chunk in xml.split("\x02"):
        if "\x01" in chunk:
            cells = [c.replace("\x03", " ").strip() for c in chunk.split("\x01")]
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                out.append("\t".join(cells))
        else:
            out.append(chunk.replace("\x03", "\n").replace("\x01", "\t"))
    return "\n".join(out)


def read_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        return docx_xml_to_text(xml)
    if ext == ".pdf":
        raise SystemExit(
            "PDF 不直接解析。请先把 PDF 转成 .docx/.txt（或用 PDF 阅读工具导出文本），再运行本脚本。"
        )
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise SystemExit("无法识别文件编码，请另存为 UTF-8 后重试。")


def split_lines(text):
    raw = text.splitlines()
    return [(i + 1, ln.rstrip()) for i, ln in enumerate(raw)]


def find_sections(lines):
    """返回 [{title,start,end,level}]，章节结束于下一个同级或更高级标题，子标题不截断章节。"""
    marks = []
    for idx, (no, ln) in enumerate(lines):
        s = ln.strip()
        m = HEADING_RE.match(s)
        if m:
            marks.append((m.group(2).strip(), idx, len(m.group(1))))
        elif CN_CHAPTER_RE.match(s) and len(s) < 60:
            marks.append((s, idx, 1))
    sections = []
    for k, (title, idx, level) in enumerate(marks):
        end = len(lines)
        for j in range(k + 1, len(marks)):
            if marks[j][2] <= level:
                end = marks[j][1]
                break
        sections.append({"title": title, "start": idx, "end": end, "level": level})
    return sections


def section_slice(lines, sections, keywords):
    for sec in sections:
        if any(k in sec["title"] for k in keywords):
            return lines[sec["start"]: sec["end"]]
    return []


def grab(pattern, text, group=1):
    m = re.search(pattern, text)
    return m.group(group).strip() if m else ""


def parse_amount(s):
    s = s.replace(",", "").replace("，", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万元|万|元)", s)
    if not m:
        return None
    v = float(m.group(1))
    return v * 10000 if m.group(2).startswith("万") else v


def extract_meta(lines, full_text):
    meta = {
        "project_name": grab(r"项目名称[：:]\s*(.+)", full_text),
        "project_no": grab(r"项目(?:编号|编码)[：:]\s*([A-Za-z0-9\-_]+)", full_text),
        "buyer": grab(r"采\s*购\s*人[：:]\s*(.+)", full_text),
        "agency": grab(r"(?:采购)?代理机构[：:]\s*(.+)", full_text),
        "budget_text": grab(r"预算金额[：:]\s*(.+)", full_text),
        "service_period": grab(r"服务期(?:限)?[：:]\s*(.+)", full_text),
        "bid_validity": grab(r"投标有效期[：:]\s*(.+)", full_text),
        "deposit_text": grab(r"投标保证金[：:]\s*(.+)", full_text),
        "scoring_method": "",
        "total_score": None,
    }
    meta["budget_amount"] = parse_amount(meta["budget_text"]) if meta["budget_text"] else None
    if "综合评分法" in full_text:
        meta["scoring_method"] = "综合评分法"
    elif "最低评标价法" in full_text or "最低价" in full_text:
        meta["scoring_method"] = "最低评标价法"
    m = re.search(r"总分\s*(\d+)\s*分", full_text) or re.search(r"满分\s*(\d+)\s*分", full_text)
    if m:
        meta["total_score"] = int(m.group(1))
    return meta


def extract_timeline(lines):
    keys = ["获取招标文件", "质疑", "澄清", "投标截止", "开标时间", "保证金", "踏勘", "答疑", "公告期限"]
    out = []
    for no, ln in lines:
        s = ln.strip()
        if not s or len(s) > 160:
            continue
        if not any(k in s for k in keys):
            continue
        if not re.search(r"\d{4}\s*[-年/\.]\s*\d{1,2}", s):
            continue
        ts = re.findall(r"\d{4}\s*[-年/\.]\s*\d{1,2}\s*[-月/\.]\s*\d{1,2}\s*日?\s*(?:\d{1,2}[:：]\d{2})?", s)
        if not ts:
            continue
        label = "时间节点"
        for k in keys:
            if k in s:
                label = k
                break
        out.append({"label": label, "time": ts[-1].strip(), "line": no, "evidence": s})
    # 去重（同一 label 保留第一条）
    seen, uniq = set(), []
    for t in out:
        if t["label"] in seen:
            continue
        seen.add(t["label"])
        uniq.append(t)
    return uniq


def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s.strip("|")
        return [c.strip() for c in s.split("|")]
    return [c.strip() for c in re.split(r"\t+|\s{2,}", s)]


def is_sep_row(cells):
    return all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells if c)


def parse_tables(lines_slice):
    """返回 [(header, rows, line_no)]，兼容 markdown 表格与 docx 转出来的制表符表格。"""
    tables, i = [], 0
    while i < len(lines_slice):
        no, ln = lines_slice[i]
        s = ln.strip()
        is_table_line = s.startswith("|") or ("\t" in s and len(split_row(s)) >= 3)
        if not is_table_line:
            i += 1
            continue
        block = []
        while i < len(lines_slice):
            no2, ln2 = lines_slice[i]
            s2 = ln2.strip()
            if not (s2.startswith("|") or ("\t" in s2 and len(split_row(s2)) >= 3)):
                break
            block.append((no2, s2))
            i += 1
        if len(block) < 2:
            continue
        header = split_row(block[0][1])
        body = [b for b in block[1:] if not is_sep_row(split_row(b[1]))]
        if body:
            tables.append((header, [(b[0], split_row(b[1])) for b in body], block[0][0]))
    return tables


def classify_scoring(text):
    # 多档分值区间（如“12—15 分 / 8—11 分”）或“优/良/一般”字样 => 主观打分
    bands = re.findall(r"\d+(?:\.\d+)?\s*[-—~至]\s*\d+(?:\.\d+)?\s*分", text)
    grade = re.search(r"(?:优|良|一般|较差|差|基本|较)[^。；]{0,8}\d+(?:\.\d+)?\s*[-—~至]\s*\d+(?:\.\d+)?\s*分", text)
    general = re.search(r"合理性|针对性|完整性|可行性|科学性|先进性|实施方案", text)
    objective = re.search(r"提供|须附|每(?:人|个|项|提供)|最多|证书|合同|验收|截图|满分|不得分|得分", text)
    if grade or len(bands) >= 2:
        return "主观"
    if general and objective:
        return "半主观"
    if general:
        return "主观"
    if objective:
        return "客观"
    return "待定"


def extract_scoring(lines, sections):
    sec = section_slice(lines, sections, ["评标办法", "评分办法", "评审办法", "评分标准"])
    warnings = []
    if not sec:
        return {"found": False, "categories": [], "items": [], "sum": 0}, ["未找到评标/评分办法章节"]

    items, categories = [], []
    pos_of_line = {no: i for i, (no, _) in enumerate(sec)}

    for idx, (no, ln) in enumerate(sec):
        s = ln.strip()
        m = re.match(
            r"^#{2,5}\s*(?:\d+(?:\.\d+)*\s*)?(.{2,10}?)\s*[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[)）]\s*$", s)
        if m:
            categories.append({
                "name": m.group(1).replace("分", "").strip(),
                "score": float(m.group(2)),
                "line": no,
                "idx": idx,
            })

    def cat_for(idx):
        best = None
        for c in categories:
            if c["idx"] < idx:
                best = c
        return best

    for header, rows, hline in parse_tables(sec):
        joined = "".join(header)
        # 先判断“这是不是一张评分表”，再去找列。反过来的话，列名一换就整表静默丢弃。
        if not re.search(r"评分|评审|评标|得分", joined):
            continue
        idx_name = next((i for i, h in enumerate(header)
                         if re.search(r"评分因素|评分项|评审因素|评分内容|评分项目|项目|内容|要素", h)), 0)
        idx_score = next((i for i, h in enumerate(header)
                          if re.search(r"分值|分数|满分|标准分|权重|分值\(分\)|分值（分）|得分", h)), None)
        idx_crit = next((i for i, h in enumerate(header)
                         if re.search(r"评分标准|评审标准|评分细则|评分说明|标准|细则|说明", h)), None)
        if idx_score is None or idx_crit is None:
            warnings.append("评分表列头无法识别（表头为「%s」），已跳过该表 %d 行，必须人工补录分值列与评分标准列"
                            % ("|".join(h.strip() for h in header), len(rows)))
            continue
        for no, cells in rows:
            if len(cells) <= max(idx_name, idx_score, idx_crit):
                continue
            name = cells[idx_name]
            score_raw = cells[idx_score]
            crit = cells[idx_crit]
            m = re.search(r"(\d+(?:\.\d+)?)", score_raw)
            score = float(m.group(1)) if m else None
            if not name and not crit:
                continue
            cat = cat_for(pos_of_line.get(no, 0))
            items.append({
                "category": cat["name"] if cat else "未分类",
                "name": name or crit[:20],
                "score": score,
                "criteria": crit,
                "type": classify_scoring(crit),
                "line": no,
                "evidence": " | ".join(cells),
            })

    # 只有文字描述、没有表格的评分项（典型：价格分），按段落兜底补一条
    filled = {it["category"] for it in items}
    for c in categories:
        if c["name"] in filled:
            continue
        para = []
        for no, ln in sec[c["idx"] + 1:]:
            s = ln.strip()
            if not s:
                if para:
                    break
                continue
            if s.startswith("#") or s.startswith("|"):
                break
            para.append(s)
        if not para:
            continue
        text = " ".join(para)
        items.append({
            "category": c["name"],
            "name": "投标报价" if c["name"] in ("价格", "报价") else c["name"],
            "score": c["score"],
            "criteria": text,
            "type": "客观",
            "line": c["line"],
            "evidence": text,
        })
        warnings.append("「%s」%g 分在原文中以文字表述、无评分表格，已按段落录入，请人工确认计分公式" % (c["name"], c["score"]))

    if not categories:
        warnings.append("未识别到“XX分（NN 分）”形式的评分分类，评分结构须人工梳理")
        # 没有分类标题时，按评分因素名兜底归册，否则目录初稿会把所有章节塞进“未分类”
        for it in items:
            if it["category"] != "未分类":
                continue
            n = it["name"] + it["criteria"]
            if re.search(r"价格|报价|费率|优惠", n):
                it["category"] = "价格"
            elif re.search(r"技术|方案|服务|实施|运维|培训|质量|进度|应急", n):
                it["category"] = "技术"
            elif re.search(r"资质|业绩|证书|信誉|财务|团队|人员|认证|奖项", n):
                it["category"] = "商务"
        if any(it["category"] != "未分类" for it in items):
            warnings.append("评分类别为脚本按因素名推断，非原文表述，归册结果须人工复核")

    total = sum(i["score"] for i in items if i["score"])
    return {"found": True, "categories": categories, "items": items, "sum": total}, warnings


def extract_star(lines, sections=None):
    # 废标章节里的“★”是在描述废标规则，不是待响应的条款，必须排除
    bad_ranges = []
    for sec in (sections or []):
        if re.search(r"废标|无效|否决", sec["title"]):
            bad_ranges.append((sec["start"], sec["end"]))

    out = []
    for idx, (no, ln) in enumerate(lines):
        s = ln.strip()
        hit = STAR_RE.search(s) or LINE_STAR_RE.match(s)
        if not hit or len(s) < 6:
            continue
        if any(a <= idx < b for a, b in bad_ranges):
            continue
        if re.search(r"未实质性响应|实质性响应.*条款.*的$", s):
            continue
        # 统一归到两档，保证下游只用判 ★/▲；原符号留在 mark 字段里备查
        mark = hit.group(0).strip() if hasattr(hit, "group") else "*"
        hard = mark in ("★", "※", "＊", "*")
        level = "★" if hard else "▲"
        out.append({
            "level": level,
            "mark": mark,
            "meaning": "实质性条款，负偏离即废标" if hard else "重要条款，通常影响评分",
            "text": s.lstrip("#").strip(),
            "line": no,
            "evidence": s,
        })
    return out


def extract_disqualify(lines, sections=None):
    """废标条款优先按“废标/无效投标”章节整章抓，其次再按关键词兜底抓散落条款。"""
    out, seen = [], set()

    def add(no, text):
        if no in seen or len(text) < 6:
            return
        seen.add(no)
        out.append({"text": text, "line": no, "evidence": text})

    for sec in (sections or []):
        if not re.search(r"废标|无效|否决", sec["title"]):
            continue
        for no, ln in lines[sec["start"]:sec["end"]]:
            s2 = ln.strip()
            if not s2 or s2.startswith("#"):
                continue
            if re.search(r"下列情形|以下情形", s2):
                continue
            # 章节标题本身不是条款（“第四章 废标条款”曾被当成一条废标理由收录）
            if CN_CHAPTER_RE.match(s2) and len(s2) < 40:
                continue
            # 编号花样：1. / 1、 / (1) / （一） / 一、 —— 全角中文数字最容易漏
            if re.match(r"^\d+[\.、\)]|^[（(][一二三四五六七八九十百\d]+[)）]|^[一二三四五六七八九十]+、", s2):
                add(no, s2.lstrip("#-* ").strip())
            elif DISQ_RE.search(s2) and len(s2) < 160:
                add(no, s2.lstrip("#-* ").strip())

    for no, ln in lines:
        s2 = ln.strip()
        if len(s2) < 8 or not DISQ_RE.search(s2) or no in seen:
            continue
        if s2.startswith("#") or re.search(r"下列情形|以下情形", s2):
            continue
        if CN_CHAPTER_RE.match(s2) and len(s2) < 40:
            continue
        add(no, s2.lstrip("#-* ").strip())

    out.sort(key=lambda x: x["line"])
    return out


def extract_format(lines, sections=None):
    """只抓“投标文件怎么做”的形式要求：份数、密封签署、编排、电子版、报价文件。

    排除 ★ 条款、评分表、废标章节，否则清单里会混进完全不相干的表头。
    """
    bad = []
    for sec in (sections or []):
        if re.search(r"废标|无效|否决|评标办法|评分办法", sec["title"]):
            bad.append((sec["start"], sec["end"]))

    out = []
    for idx, (no, ln) in enumerate(lines):
        s = ln.strip()
        if len(s) < 5 or len(s) > 200:
            continue
        if any(a <= idx < b for a, b in bad):
            continue
        if STAR_RE.search(s) or s.startswith("#") or s.startswith("|") or s.count("\t") >= 2:
            continue
        if re.match(r"^\d+(\.\d+)*\s*$", s) or (re.match(r"^\d+(\.\d+)*\s", s) and len(s) < 20):
            continue
        bucket = None
        for name, kws in FORMAT_BUCKETS:
            if any(k in s for k in kws):
                bucket = name
                break
        if not bucket:
            continue
        if bucket == "份数" and not re.search(r"\d+\s*份|份数[:：]|一正", s):
            continue
        out.append({"type": bucket, "text": s.lstrip("#-* ").strip(), "line": no})
    return out


def extract_qualification(lines, sections):
    sec = section_slice(lines, sections, ["资格要求", "投标人资格", "资格条件", "合格投标人"])
    if not sec:
        return []
    out = []
    for no, ln in sec:
        s = ln.strip()
        if len(s) < 8:
            continue
        if re.match(r"^\d+[\.、\)]|^[（(]\d+[)）]|^[★▲]", s):
            out.append({
                "text": s.lstrip("#-* ").strip(),
                "is_star": bool(STAR_RE.search(s)),
                "line": no,
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    if not os.path.exists(args.source):
        raise SystemExit("文件不存在: %s" % args.source)

    text = read_text(args.source)
    lines = split_lines(text)
    sections = find_sections(lines)

    warnings = []
    meta = extract_meta(lines, text)
    if not meta["project_name"]:
        warnings.append("未识别到项目名称，请人工补充")

    scoring, sw = extract_scoring(lines, sections)
    warnings += sw

    if scoring["found"] and meta["total_score"] and scoring["sum"]:
        if abs(scoring["sum"] - meta["total_score"]) > 0.01:
            warnings.append(
                "评分表分项合计 %.1f 分，与文件声明的总分 %s 分不一致，必须人工核对（常见于分值列被合并）"
                % (scoring["sum"], meta["total_score"])
            )
    if scoring["found"] and not scoring["items"]:
        warnings.append("评分表未解析出任何评分点，必须人工补录")

    star = extract_star(lines, sections)
    disq = extract_disqualify(lines, sections)
    fmt = extract_format(lines, sections)
    qual = extract_qualification(lines, sections)
    timeline = extract_timeline(lines)

    if not star:
        warnings.append("未发现 ★/▲ 条款，请确认该文件是否使用其他标记（如“*”“必须”“应”）")
    else:
        soft = [s for s in star if s.get("mark") not in ("★", "▲")]
        if soft:
            warnings.append("有 %d 条实质性条款使用了非标准标记（%s），已按★处理，请核对原文符号"
                            % (len(soft), "、".join(sorted({s["mark"] for s in soft}))))
    # 无符号但属强制表述的条款只做提醒，不自动当★（否则噪音会淹没真条款）
    mand = [(no, ln.strip()) for no, ln in lines
            if MANDATORY_RE.search(ln) and not STAR_RE.search(ln) and not LINE_STAR_RE.match(ln.strip())]
    if mand and len(mand) <= len(lines) * 0.4:
        warnings.append("另发现 %d 条含“必须/应当/须”的强制表述但未标★，请逐条判断是否属实质性要求（首条见 L%d）"
                        % (len(mand), mand[0][0]))
    if not disq:
        warnings.append("未发现废标/无效投标条款，务必人工在原文中确认")
    if not timeline:
        warnings.append("未识别到时间节点，请人工从招标公告中摘录")

    parsed = {
        "meta": meta,
        "source": os.path.abspath(args.source),
        "line_count": len(lines),
        "sections": [{"title": s["title"], "line": lines[s["start"]][0]} for s in sections],
        "scoring": scoring,
        "star_clauses": star,
        "disqualify_clauses": disq,
        "format_requirements": fmt,
        "qualification": qual,
        "timeline": timeline,
        "warnings": warnings,
        "human_review_required": True,
    }

    out = args.out or os.path.splitext(args.source)[0] + ".parsed.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)

    print("解析完成 -> %s" % out)
    print("  评分点 %d 个，合计 %.1f 分（声明 %s 分）"
          % (len(scoring["items"]), scoring["sum"], meta["total_score"]))
    print("  ★条款 %d 条，▲条款 %d 条，废标条款 %d 条"
          % (sum(1 for s in star if s["level"] == "★"), sum(1 for s in star if s["level"] == "▲"), len(disq)))
    print("  格式要求 %d 条，资格条件 %d 条，时间节点 %d 个" % (len(fmt), len(qual), len(timeline)))
    if warnings:
        print("  警告 %d 条：" % len(warnings))
        for w in warnings:
            print("    - " + w)


if __name__ == "__main__":
    main()
