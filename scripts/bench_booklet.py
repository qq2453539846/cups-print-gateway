#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小册子预览到底慢在哪 —— 分段计时，用来判断优化该往哪儿使劲。

    python3 bench_booklet.py                # 10 页 A4，横扫 300/200/150/100 dpi
    python3 bench_booklet.py --pages 20 --dpi 300,150

测三段，因为它们的耗时来源完全不同：

  build_final  → impose 栅格拼版：按「纸面目标 dpi」**反算源页 dpi** 再渲染，
                 小册子是 2 版/面，源页要放大到纸面一半，于是源页 dpi 比目标还高；
  make_preview → 把生成的最终 PDF 按 72dpi 渲染成预览 PNG（页数 = 纸面数）；
  compose      → 把渲染好的位图合成到纸上并写 PDF。

只在设备（或任何装了 poppler-utils 的机器）上跑。
"""
import argparse
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pg_engine                                                    # noqa: E402
import pg_layout                                                    # noqa: E402
import make_testfiles                                               # noqa: E402

WORK = "/tmp/bk-bench"


def timed(fn, *a, **kw):
    t0 = time.time()
    res = fn(*a, **kw)
    return time.time() - t0, res


def run(pages, dpi, keep=False):
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    src = os.path.join(WORK, "src.pdf")
    make_testfiles.make_pdf(src, pages=pages)

    spec = pg_engine.PrintSpec.from_form({
        "paper": "A4", "scale_mode": "fit", "dpi": dpi,
        "booklet": True, "duplex": "two-sided-long-edge",
    })
    spec.validate()
    work = os.path.join(WORK, "w")
    os.makedirs(work)

    t_build, build = timed(pg_engine.build_final, spec, src, work)
    t_prev, images = timed(pg_engine.make_preview, build["pdf"], work)

    # 拆开看 build 里面哪一段占大头。注意渲染的输入是**归一化后的源 PDF**、
    # 输出是最终 PDF —— render_source_pages 的 -f/-l 用的是源页号，
    # 拿最终 PDF 去渲染会报「第 10 页超出最后一页 6」。
    norm = os.path.join(work, "normalized.pdf")
    plan = dict(build["plan"])
    plan["pages_used"] = plan.get("pages_used") or len(plan["_page_sizes"])
    jobs = pg_layout.render_jobs(plan, dpi)
    try:
        t_render, imgs = timed(pg_engine.render_source_pages, norm, jobs,
                               work, False, None, dpi, len(jobs), "")
        t_comp, _ = timed(pg_layout.compose, plan, imgs,
                          os.path.join(work, "again.pdf"))
        rdpi = sorted({j["dpi"] for j in jobs})
    except Exception as exc:                                    # noqa: BLE001
        t_render = t_comp = float("nan")
        rdpi = ["拆解失败：%s" % exc]
    px = sum((j["region"][2] * j["region"][3]) for j in jobs)
    print("  dpi=%-4d 页=%-3d  总计 %6.1fs │ 拼版 %6.1fs（渲染 %6.1fs + 合成 %5.1fs）"
          " │ 预览 %5.1fs │ 源页 dpi %s │ 渲染像素 %5.1f MP"
          % (dpi, pages, t_build + t_prev, t_build, t_render, t_comp, t_prev,
             rdpi, px / 1e6))
    if not keep:
        shutil.rmtree(WORK, ignore_errors=True)
    return t_build + t_prev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=10)
    ap.add_argument("--dpi", default="300,200,150,100")
    ap.add_argument("--keep", action="store_true", help="保留中间文件便于排查")
    args = ap.parse_args()

    dpi_list = [int(x) for x in args.dpi.split(",") if x.strip()]
    print("小册子预览分段计时（页数 %d）：" % args.pages)
    for d in dpi_list:
        run(args.pages, d, args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
