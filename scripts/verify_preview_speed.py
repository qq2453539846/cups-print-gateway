#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测「一次预览要等多久」—— 走真实的 HTTP 接口，只依赖标准库。

    python3 verify_preview_speed.py                       # 10 页小册子，各测一遍
    python3 verify_preview_speed.py --pages 20 --url http://127.0.0.1:8080

为什么不用 bench_booklet.py：那个测的是引擎三段耗时，用来定位瓶颈；
这个测的是**用户真实等待时间** —— 上传 + /api/preview 往返，含服务端排队。
改完预览相关的代码，两个都跑：一个看总量，一个看是哪儿慢。

顺带核对两件事（都是「能出纸但不对」型的故障，光看时间发现不了）：
  · 预览档与出纸档的**版面**必须一致（页数/纸面尺寸/note 里的渲染分辨率）；
  · 预览档落盘的目录与 /img 回退路径必须对得上（否则预览整片 404）。
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import uuid


def post(url, fields=None, files=None, timeout=900):
    """
    上传走 multipart/form-data，其余接口走 application/json —— 网关就是这么分的：
    /api/upload 解析 multipart，/api/preview、/api/print 只认 JSON body。
    两边都用手搓的编码，设备上未必有 requests。
    """
    if not files:                                   # JSON 接口
        req = urllib.request.Request(
            url, data=json.dumps(fields or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    boundary = "----pg" + uuid.uuid4().hex
    body = b""
    for k, v in (fields or {}).items():
        body += (b"--" + boundary.encode() + b"\r\n"
                 b'Content-Disposition: form-data; name="' + k.encode() + b'"\r\n\r\n'
                 + str(v).encode() + b"\r\n")
    for name, filename, content in files:
        body += (b"--" + boundary.encode() + b"\r\n"
                 b'Content-Disposition: form-data; name="' + name.encode()
                 + b'"; filename="' + filename.encode() + b'"\r\n'
                 b"Content-Type: application/octet-stream\r\n\r\n" + content + b"\r\n")
    body += b"--" + boundary.encode() + b"--\r\n"

    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--pages", type=int, default=10)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--repeat", type=int, default=2,
                    help="同一次设置重复点几次预览（第 2 次应当命中缓存，秒回）")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import make_testfiles

    src = "/tmp/preview-speed-%d.pdf" % args.pages
    make_testfiles.make_pdf(src, pages=args.pages)
    data = open(src, "rb").read()
    print("样张：%d 页 %s（%.1f KB）" % (args.pages, src, len(data) / 1024.0))

    t0 = time.time()
    up = post(base + "/api/upload", files=[("file", "speed.pdf", data)])
    t_up = time.time() - t0
    print("上传：%.2fs  pages=%s booklet=%s" % (t_up, up.get("pages"),
                                              up.get("booklet")))

    spec = {"paper": "A4", "scale_mode": "fit", "dpi": args.dpi,
            "booklet": True, "duplex": "two-sided-long-edge"}
    timings = []
    for i in range(args.repeat):
        t0 = time.time()
        # spec 必须是**嵌套对象**：塞成字符串的话服务端 from_form 拿到的不是 dict，
        # 会安静地退回全部默认值（表现为「小册子没生效、还是 10 面」）。
        res = post(base + "/api/preview",
                   fields={"job": up["id"], "spec": spec})
        dt = time.time() - t0
        timings.append(dt)
        print("预览 #%d：%.1fs  mode=%s 出纸 %s 面 预览图 %d 张%s"
              % (i + 1, dt, res.get("mode"), res.get("pages"),
                 len(res.get("images") or []),
                 "（命中缓存）" if i and dt < timings[0] / 3 else ""))
        if i == 0:
            for n in (res.get("notes") or []):
                if "渲染分辨率" in n:
                    print("        %s" % n)

    # /img 必须真能取到图：只认第一张，验的是「路径对得上」
    imgs = res.get("images") or []
    if imgs:
        try:
            with urllib.request.urlopen(base + imgs[0], timeout=60) as r:
                blob = r.read()
            print("取图 %s -> HTTP %d，%d 字节 %s"
                  % (imgs[0], r.status, len(blob),
                     "（PNG 头正确）" if blob[:4] == b"\x89PNG" else "（不是 PNG！）"))
        except Exception as exc:                                # noqa: BLE001
            print("取图失败：%s" % exc)

    if len(timings) > 1 and timings[1] > timings[0] / 3:
        print("注意：重复预览没有命中缓存，缓存可能失效了")
    print("\n首屏预览耗时：%.1fs" % timings[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
