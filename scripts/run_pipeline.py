#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键跑完标书初稿流水线：解析 → 匹配 → 出四份交付物。

用法:
    python run_pipeline.py <招标文件> <材料库.json> [-o 工作目录]

等价于依次执行 tender_parse.py / material_match.py / render_deliverables.py。
中间文件 parsed.json、matched.json 保留在工作目录，便于人工修正后重跑后两步。
"""

import argparse
import os
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))


def run(script, args):
    cmd = [sys.executable, os.path.join(HERE, script)] + args
    print("\n$ " + " ".join(cmd[1:]))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        print(r.stdout or "")
        print(r.stderr or "")
        raise SystemExit("步骤失败：%s（详见上方输出）" % script)
    print(r.stdout.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tender")
    ap.add_argument("library")
    ap.add_argument("-o", "--outdir", default="bid_work")
    args = ap.parse_args()

    work = os.path.abspath(args.outdir)
    os.makedirs(work, exist_ok=True)
    parsed = os.path.join(work, "parsed.json")
    matched = os.path.join(work, "matched.json")
    out = os.path.join(work, "output")

    run("tender_parse.py", [args.tender, "-o", parsed])
    run("material_match.py", [parsed, args.library, "-o", matched])
    run("render_deliverables.py", [parsed, matched, "-o", out])

    print("\n完成。工作目录：%s" % work)
    print("下一步：读 04_自查清单.md 的 A 区，确认废标红线；再按 03_目录初稿.md 分工。")


if __name__ == "__main__":
    main()
