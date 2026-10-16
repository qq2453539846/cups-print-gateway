#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
预览档验证：**同一个设置，按预览分辨率构建与按出纸分辨率构建，预览图必须几乎一样**。

    python3 verify_preview_dpi.py                 # 设备上跑，约 5 分钟
    python3 verify_preview_dpi.py --only 小册子
    python3 verify_preview_dpi.py --pages 6 --verbose

为什么需要它：v3.4 起预览不再按出纸分辨率（默认 300dpi）构建，而是走
`PREVIEW_BUILD_DPI`（144）。省时间的原理是「预览图最后只有 72dpi」——
但这个前提一旦不成立（版面几何开始依赖 dpi、或者某条路径忘了传 preview），
用户看到的版面就会和印出来的不是一回事，而且是**静默**的。

三层断言，从硬到软：

1. **结构性相等（精确）**：出纸面数、纸面尺寸、mode、notes、预览图张数与逐张尺寸。
   这几项一个字节都不能差 —— 它们不对就是真坏了。
2. **没有位移（这才是关键）**：把其中一张按 ±1 像素的 9 个位置分别对齐再比，
   **偏移 (0,0) 必须是差异最小的那个**。版面错位 1 个像素都会让这条挂掉。
3. **差异幅度（烟雾报警）**：只允许抗锯齿级差异 —— 均差、以及差异大的像素占比。

> 为什么不用「差异大的像素占比」当主判据：文字边缘的抗锯齿差异会让像素在
> 白↔黑之间翻转，单个像素的差值本来就很大（实测 1~2% 的像素差值 >32）。
> 拿它当阈值只会逼着人把阈值放宽，最后什么也拦不住。位移判据才分辨得出
> 「边缘软了一点」和「整版挪了一格」。

> 判据实现上的坑（2026-09-18 踩过）：零偏移 (0,0) 本身就是 9 个候选之一，
> 所以 best_off==(0,0) 时 mean0 == best 恒成立。曾经写成
> `elif mean0 > best * MARGIN` 来判断「是否位移」，结果零偏移最优时也必然触发，
> 小样张直接误报 FAIL。正确写法是反过来的：只有**某个 ±1 偏移明显更优**
> （gain = (mean0-best)/mean0 超过阈值，且绝对改善不为 0）才算位移。

覆盖所有会走栅格拼版的路径：多版/张、小册子、分割打印、水印（装饰层）、
拉伸铺满 —— 它们的 render_jobs 计算方式各不相同。
"""
import argparse
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pg_engine                                                    # noqa: E402
import make_testfiles                                               # noqa: E402

MEAN_TOL = 3.0            # 平均像素差上限（0-255）—— 烟雾报警
BIG_TOL = 8.0             # 差 >32 的像素占比上限（%）—— 烟雾报警
SHIFT_GAIN = 0.10         # 某个 ±1 偏移比零偏移好 10% 以上 → 判定为内容位移
SHIFT_MIN_GAIN = 0.05     # 且绝对改善要 >0.05/255，免得在近乎空白页上被噪声骗到

CASES = [
    ("小册子", {"booklet": True, "duplex": "two-sided-long-edge"}),
    ("2版/张", {"per_sheet": 2}),
    ("4版/张", {"per_sheet": 4}),
    ("水印+页码", {"per_sheet": 2, "decor": {"wm_enabled": True, "wm_text": "内部",
                                            "pn_enabled": True, "pn_format": "n"}}),
    ("分割 2x2", {"split_rows": 2, "split_cols": 2}),
    ("拉伸铺满", {"scale_mode": "stretch", "per_sheet": 2}),
]


def shots(spec, src, work, preview):
    os.makedirs(work, exist_ok=True)
    res = pg_engine.build_final(spec, src, work, preview=preview)
    return res, pg_engine.make_preview(res["pdf"], work)


def _mean_abs_diff(a, b, dx, dy):
    """把 b 平移 (dx,dy) 后与 a 比，只比两者重叠的区域（不做环绕）。"""
    from PIL import ImageChops
    w, h = a.size
    ca = a.crop((max(0, dx), max(0, dy), w + min(0, dx), h + min(0, dy)))
    cb = b.crop((max(0, -dx), max(0, -dy), w + min(0, -dx), h + min(0, -dy)))
    hist = ImageChops.difference(ca, cb).histogram()
    total = float(sum(hist))
    return sum(i * n for i, n in enumerate(hist)) / total, hist, total


def compare(pa, pb, verbose=False):
    """返回 (问题列表, 零偏移均差, 最优偏移, 最大偏移增益)。"""
    from PIL import Image
    ia, ib = Image.open(pa).convert("L"), Image.open(pb).convert("L")
    if ia.size != ib.size:
        return ["尺寸不同 %s vs %s" % (ia.size, ib.size)], 0.0, None, 0.0

    means = {}
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            means[(dx, dy)] = _mean_abs_diff(ia, ib, dx, dy)[0]
    mean0 = means[(0, 0)]
    best_off = min(means, key=lambda k: means[k])
    best = means[best_off]
    # 零偏移本身就是 9 个候选之一 → mean0 == best ⟺ best_off == (0,0)，gain 恒为 0
    gain = (mean0 - best) / mean0 if mean0 > 0 else 0.0

    _, hist, total = _mean_abs_diff(ia, ib, 0, 0)
    big = sum(n for i, n in enumerate(hist) if i > 32) / total * 100.0

    if verbose:
        print("      · %s 零偏移 %.3f / 最优 %s %.3f / gain %.1f%% / 差>32 %.2f%%"
              % (os.path.basename(pa), mean0, best_off, best, gain * 100, big))

    bad = []
    if mean0 > MEAN_TOL:
        bad.append("均差 %.2f/255 超限" % mean0)
    if big > BIG_TOL:
        bad.append("差>32 的像素 %.1f%% 超限" % big)
    # 零偏移必须是全场最优。只有「某个 ±1 偏移明显更优」才说明版面真的挪了位置；
    # 零偏移最优（gain==0）直接通过。
    if best_off != (0, 0) and gain > SHIFT_GAIN and (mean0 - best) > SHIFT_MIN_GAIN:
        bad.append("最优对齐偏移是 %s（零偏移 %.2f → 偏移后 %.2f，增益 %.0f%%）"
                   % (best_off, mean0, best, gain * 100))
    return bad, mean0, best_off, gain


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=10)
    ap.add_argument("--only", default="", help="只跑名字里含该子串的用例")
    ap.add_argument("--verbose", action="store_true", help="打印每张预览图的 9 向对齐数据")
    args = ap.parse_args()

    for tool, name in ((pg_engine.PDFTOPPM, "pdftoppm"), (pg_engine.GS, "gs")):
        if not pg_engine.which(tool):
            print("找不到 %s，这脚本只能在装了 poppler/ghostscript 的机器上跑" % name)
            return 2

    root = tempfile.mkdtemp(prefix="pgpvdpi-")
    src = os.path.join(root, "src.pdf")
    make_testfiles.make_pdf(src, pages=args.pages)
    print("样张 %d 页；预览档 dpi=%d，出纸档 dpi=%d\n"
          % (args.pages, pg_engine.PREVIEW_BUILD_DPI, pg_engine.DEFAULT_DPI))

    fails = 0
    for name, over in CASES:
        if args.only and args.only not in name:
            continue
        spec = pg_engine.PrintSpec.from_form(dict({"paper": "A4",
                                                   "scale_mode": "fit",
                                                   "dpi": pg_engine.DEFAULT_DPI},
                                                  **over))
        spec.validate()
        tag = os.path.join(root, name.replace("/", "_"))
        try:
            hi, hi_imgs = shots(spec, src, os.path.join(tag, "hi"), False)
            lo, lo_imgs = shots(spec, src, os.path.join(tag, "lo"), True)
        except Exception as exc:                                # noqa: BLE001
            print("  %-10s FAIL 构建失败：%s" % (name, exc))
            fails += 1
            continue

        bad = []
        if hi["pages"] != lo["pages"]:
            bad.append("出纸面数 %s vs %s" % (hi["pages"], lo["pages"]))
        if hi["sheet"] != lo["sheet"]:
            bad.append("纸面尺寸 %s vs %s" % (hi["sheet"], lo["sheet"]))
        if hi["mode"] != lo["mode"]:
            bad.append("模式 %s vs %s" % (hi["mode"], lo["mode"]))
        if hi["notes"] != lo["notes"]:
            bad.append("notes 不同")
        if len(hi_imgs) != len(lo_imgs):
            bad.append("预览图张数 %d vs %d" % (len(hi_imgs), len(lo_imgs)))

        worst, worst_gain, worst_big = 0.0, 0.0, 0.0
        for a, b in zip(hi_imgs, lo_imgs):
            why, mean, off, gain = compare(a, b, args.verbose)
            worst = max(worst, mean)
            worst_gain = max(worst_gain, gain)
            if why:
                bad.append("%s：%s" % (os.path.basename(a), "；".join(why)))
                break

        if bad:
            fails += 1
            print("  %-10s FAIL %s" % (name, "；".join(bad[:2])))
        else:
            print("  %-10s OK   %s 面 / 预览 %d 张 / 纸面 %s / 最大均差 %.2f/255 / "
                  "最大偏移增益 %.1f%%"
                  % (name, hi["pages"], len(hi_imgs),
                     "%g×%g" % hi["sheet"], worst, worst_gain * 100))

    shutil.rmtree(root, ignore_errors=True)
    print("\n%s（%d 个用例失败）" % ("全部通过" if not fails else "有失败", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
