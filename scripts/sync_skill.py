#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把开发目录里这一份代码同步进 skill（`~/.workbuddy/skills/cups-print-gateway`）。

    python3 sync_skill.py            # 同步并打印每份文件的变更摘要
    python3 sync_skill.py --check    # 只报告差异，不写盘（CI / 交付前体检用）
    python3 sync_skill.py --zip      # 同步后顺手重新打包 skill 压缩包

为什么要有它：这个项目有**四份副本**（开发目录 → skill → GitHub → 设备），
手工 `cp` 容易漏 —— 上一次漏掉的就是 `print_gateway.py` 本身，
skill 里还停着旧版（面板、单测条数都对不上），而没人会去比对文件大小。

映射写在下面这张表里，改了文件名记得同步改这里。

> 注意：本表是**单向**的（开发目录 → skill）。skill 的 `references/` 与 `SKILL.md`
> 只在 skill 里维护，开发目录没有对应副本 —— 所以这两处要改就直接改 skill，
> 别指望同步脚本。反过来，`scripts/` 下的**每一个文件都必须出现在本表里**，
> 否则它会在 skill 里悄悄停成旧版（2026-09-18 又漏了三个验证脚本，已补）。
"""
import argparse
import hashlib
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.expanduser("~/.workbuddy/skills/cups-print-gateway")
OUT_DIR = os.path.join(HERE, "..", "outputs")
ZIP_NAME = "cups-print-gateway.zip"

# 项目里的文件 -> skill 里的相对路径（相对 skill 根）
MAP = [
    # 运行时
    ("print_gateway.py", "scripts/print_gateway.py"),
    ("pg_engine.py", "scripts/pg_engine.py"),
    ("pg_layout.py", "scripts/pg_layout.py"),
    ("pg_decor.py", "scripts/pg_decor.py"),
    ("pg_dns.py", "scripts/pg_dns.py"),
    ("pg_admin.py", "scripts/pg_admin.py"),
    ("pg_ddns.py", "scripts/pg_ddns.py"),
    ("pg_sticker.py", "scripts/pg_sticker.py"),
    ("gen_qr.py", "scripts/gen_qr.py"),
    ("gw.ppd", "scripts/gw.ppd"),
    ("setup_printer.py", "scripts/setup_printer.py"),
    ("snmp_tray.py", "scripts/snmp_tray.py"),
    ("deploy_gateway.py", "scripts/deploy_gateway.py"),
    ("get_hostkey.py", "scripts/get_hostkey.py"),
    ("make_testfiles.py", "scripts/make_testfiles.py"),
    # 测试
    ("test_backend/gwtest", "scripts/test_backend/gwtest"),
    ("test_pg_engine.py", "scripts/test_pg_engine.py"),
    ("test_print_gateway.py", "scripts/test_print_gateway.py"),
    ("test_gen_qr.py", "scripts/test_gen_qr.py"),
    ("test_pg_dns.py", "scripts/test_pg_dns.py"),
    ("test_pg_admin.py", "scripts/test_pg_admin.py"),
    ("test_pg_ddns.py", "scripts/test_pg_ddns.py"),
    ("test_pg_sticker.py", "scripts/test_pg_sticker.py"),
    # 验证
    ("verify_e2e.py", "scripts/verify_e2e.py"),
    ("verify_preview.py", "scripts/verify_preview.py"),
    ("verify_qr.py", "scripts/verify_qr.py"),
    ("verify_qr_ref.py", "scripts/verify_qr_ref.py"),
    ("diagnose_png.py", "scripts/diagnose_png.py"),
    ("diagnose_decode.py", "scripts/diagnose_decode.py"),
    ("verify_booklet_ui.js", "scripts/verify_booklet_ui.js"),
    ("verify_panel_layout.py", "scripts/verify_panel_layout.py"),
    ("verify_preview_dpi.py", "scripts/verify_preview_dpi.py"),
    ("verify_preview_speed.py", "scripts/verify_preview_speed.py"),
    ("verify_img_auth.py", "scripts/verify_img_auth.py"),
    ("verify_sticker.py", "scripts/verify_sticker.py"),
    ("bench_booklet.py", "scripts/bench_booklet.py"),
    ("make_booklet_demo.py", "scripts/make_booklet_demo.py"),
    # 文档
    ("README.md", "scripts/README.md"),
    # 同步工具自身（跟着走，别让它脱队）
    ("sync_skill.py", "scripts/sync_skill.py"),
]


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只报告差异，不写盘")
    ap.add_argument("--zip", action="store_true", help="同步后重新打包 skill")
    ap.add_argument("--skill", default=SKILL, help="skill 根目录")
    args = ap.parse_args()

    if not os.path.isdir(args.skill):
        sys.exit("找不到 skill 目录：%s" % args.skill)

    changed = same = missing = 0
    for rel_src, rel_dst in MAP:
        src = os.path.join(HERE, rel_src)
        dst = os.path.join(args.skill, rel_dst)
        if not os.path.exists(src):
            print("  缺失源文件  %s" % rel_src)
            missing += 1
            continue
        if os.path.exists(dst) and md5(src) == md5(dst):
            same += 1
            continue
        arrow = "新增" if not os.path.exists(dst) else "更新"
        print("  %s  %s -> %s" % (arrow, rel_src, rel_dst))
        changed += 1
        if not args.check:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

    print("\n同步结果：更新 %d、一致 %d、缺失 %d（%s）"
          % (changed, same, missing, "仅检查" if args.check else "已写盘"))

    if args.check:
        return 1 if changed or missing else 0

    if args.zip:
        os.makedirs(OUT_DIR, exist_ok=True)
        zp = os.path.join(OUT_DIR, ZIP_NAME)
        n = 0
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(args.skill):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in files:
                    if f.endswith(".pyc"):
                        continue
                    p = os.path.join(root, f)
                    z.write(p, os.path.relpath(p, args.skill))
                    n += 1
        print("已打包：%s（%d 个文件，%.1f KB，md5 %s）"
              % (zp, n, os.path.getsize(zp) / 1024.0, md5(zp)[:8]))

    return 0


if __name__ == "__main__":
    sys.exit(main())
