#!/usr/bin/env python3
"""
预览并行渲染验证 —— 在设备上跑（需要 pdftoppm 与真实 PDF）。

用法：
    python3 verify_preview.py [PDF] [--dir /opt/print-gateway] [--repeat 3]

对比 `workers=1`（单进程单次调用，等价改动前的实现）与
`workers=PREVIEW_WORKERS`（默认 3，留 1 核）三件事：

  1. 墙钟耗时与提速倍数
  2. 输出页数与页号顺序（并行分片不能改变对外返回的顺序）
  3. 每页 PNG 的 md5 —— 并行渲染必须与单进程逐字节一致

第 3 条是这次改动的核心安全性证明：分片只改「谁来渲染哪几页」，
不该改变任何一页的渲染结果。

加 `--concurrent N` 还能验证全局限流：同时发 N 个预览请求，
采样 /proc 里正在跑的 pdftoppm 进程数，峰值不得超过 PREVIEW_WORKERS
—— 这是「4 核留 1 核」在多人同时使用时的实际保证。
"""

import argparse
import hashlib
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

ap = argparse.ArgumentParser()
ap.add_argument("pdf", nargs="?", default=os.path.join(HERE, "sample_multipage.pdf"),
                help="用于测试的多页 PDF")
ap.add_argument("--dir", default=HERE, help="pg_engine.py 所在目录")
ap.add_argument("--repeat", type=int, default=3, help="每种模式重复次数，取最快")
ap.add_argument("--workers", type=int, default=0, help="并行度，0 表示用模块默认")
ap.add_argument("--concurrent", type=int, default=0,
                help="并发请求数，用于验证全局限流（0 表示不测）")
args = ap.parse_args()

sys.path.insert(0, args.dir)
import pg_engine as pg                                     # noqa: E402

WORKERS = args.workers or pg.PREVIEW_WORKERS


def pdftoppm_live() -> int:
    """此刻正在跑的 pdftoppm 进程数（读 /proc/comm，不依赖 pgrep）。"""
    live = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % entry) as fh:
                if fh.read().strip() == "pdftoppm":
                    live += 1
        except OSError:
            pass
    return live


def concurrent_check(requests: int) -> int:
    """同时发 N 个预览请求，确认在跑的 pdftoppm 总数没超过 PREVIEW_WORKERS。"""
    peak = [0]
    outcomes: list[object] = []
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            peak[0] = max(peak[0], pdftoppm_live())
            time.sleep(0.02)

    def one_request():
        work = tempfile.mkdtemp(prefix="prev_cc_")
        try:
            pg.make_preview(args.pdf, work)
            outcomes.append(True)
        except Exception as exc:                           # noqa: BLE001
            outcomes.append(exc)

    sampler_thread = threading.Thread(target=sampler)
    callers = [threading.Thread(target=one_request) for _ in range(requests)]
    sampler_thread.start()
    started = time.perf_counter()
    for t in callers:
        t.start()
    for t in callers:
        t.join()
    elapsed = time.perf_counter() - started
    stop.set()
    sampler_thread.join()

    failures = [o for o in outcomes if o is not True]
    ok_peak = peak[0] <= pg.PREVIEW_WORKERS
    print("并发请求 %d 个    总耗时 %.2fs    全部成功 %s"
          % (requests, elapsed, "是" if not failures else "否"))
    print("pdftoppm 峰值     %d（上限 %d）    %s"
          % (peak[0], pg.PREVIEW_WORKERS, "OK" if ok_peak else "超限！"))
    for failure in failures:
        print("  失败：%s" % failure)
    return 0 if (not failures and ok_peak) else 1


def once(workers: int):
    """跑一轮预览，返回 (耗时, 页号列表, md5 列表)。"""
    work = tempfile.mkdtemp(prefix="prev_w%d_" % workers)
    start = time.perf_counter()
    files = pg.make_preview(args.pdf, work, workers=workers)
    elapsed = time.perf_counter() - start
    nums, digests = [], []
    for path in files:
        nums.append(int(os.path.basename(path).rsplit("-", 1)[1].split(".")[0]))
        with open(path, "rb") as fh:
            digests.append(hashlib.md5(fh.read()).hexdigest())
    return elapsed, nums, digests


def main() -> int:
    if not os.path.exists(args.pdf):
        print("!! 找不到测试 PDF：%s" % args.pdf)
        return 2
    total, _ = pg.pdf_info(args.pdf)
    limit = min(total, pg.PREVIEW_MAX_PAGES)
    print("测试 PDF      : %s（%d 页，预览渲染 %d 页）" % (args.pdf, total, limit))
    print("预览 dpi      : %d" % pg.PREVIEW_DPI)
    print("CPU 核数      : %s" % os.cpu_count())
    print("并行度        : 单进程 1  vs  并行 %d（留 %d 核给服务）"
          % (WORKERS, max(0, (os.cpu_count() or 1) - WORKERS)))
    print("分片方案      : %s" % (pg._preview_chunks(limit, WORKERS),))
    print()

    result = {}
    for workers in (1, WORKERS if WORKERS != 1 else 1):
        runs = [once(workers) for _ in range(max(1, args.repeat))]
        runs.sort(key=lambda r: r[0])
        best, nums, digests = runs[0]
        result[workers] = (best, nums, digests)
        print("workers=%-2d  最快 %6.2fs   各轮 %s   页数 %d   页号升序 %s"
              % (workers, best, " ".join("%.2f" % r[0] for r in runs), len(nums),
                 nums == sorted(nums)))
    print()

    code = 0
    if 1 in result and WORKERS in result and WORKERS != 1:
        base_dt, base_nums, base_md5 = result[1]
        fast_dt, fast_nums, fast_md5 = result[WORKERS]
        ok_pages = base_nums == fast_nums
        ok_bytes = base_md5 == fast_md5
        print("页号序列一致  : %s" % ("是" if ok_pages else "否  %s vs %s" % (base_nums, fast_nums)))
        print("逐字节一致    : %s" % ("是" if ok_bytes else "否"))
        print("提速          : %.2fx（%.2fs -> %.2fs）" % (base_dt / fast_dt, base_dt, fast_dt))
        if not ok_bytes:
            for i, (a, b) in enumerate(zip(base_md5, fast_md5)):
                if a != b:
                    print("  首个不一致页：第 %d 页 %s vs %s" % (i + 1, a[:12], b[:12]))
                    break
        code = 0 if (ok_pages and ok_bytes) else 1

    if args.concurrent > 1:
        print()
        code = max(code, concurrent_check(args.concurrent))
    return code


if __name__ == "__main__":
    sys.exit(main())
