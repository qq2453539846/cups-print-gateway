#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发票邮件自动打印守护进程（纯标准库，无第三方依赖）。

流程：轮询专用 IMAP 邮箱 -> 白名单/类型门禁 -> 取 PDF 附件 ->
      按文件名/正文关键词分类（顺丰电子发票无明细→A5 单面；含运单明细或运单明细→A4 双面）->
      pdftotext 抽文字 + 关键词门禁（确认是发票才打）-> 提取发票号记录 ->
      调打印网关 /api/upload + /api/print（套用对应预设）-> 失败进 fail 目录。
      首启不打印邮箱历史，只打印启动后新收到的（print_only_new）。

护栏（务必配置）：
  - imap + 白名单发件人：否则任何人都能触发出纸
  - 仅 PDF + 大小上限
  - Message-ID 去重：轮询重叠不会重复打印
  - dry_run：先 dry-run 观察分类/门禁结果，再真正接打印机

配置示例见文件末尾 DEFAULT_CONFIG。运行：
  python invoice_mailer.py --config invoice_mailer.json --once
  python invoice_mailer.py --config invoice_mailer.json --dry-run
  python invoice_mailer.py --config invoice_mailer.json --self-test
  python invoice_mailer.py --config invoice_mailer.json --classify-file 某发票.pdf
  python invoice_mailer.py --config invoice_mailer.json --print-unit
"""
import argparse
import email
from email.header import make_header, decode_header
import imaplib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_CONFIG = {
    # 打印网关地址（dev-10 上的 print_gateway.py）
    "gateway": "http://127.0.0.1:8080",
    "token": "",                       # 网关若带 --token，填这里；会作为 ?t= 附加
    # IMAP 收件
    "imap": {
        "host": "imap.example.com", "port": 993, "ssl": True,
        "user": "invoices@example.com", "password": "CHANGE_ME"
    },
    "imap_timeout": 60,                # IMAP 连接/命令 socket 超时（秒），防空连接挂死
    "mailbox": "INBOX",
    # 安全护栏
    "whitelist": ["@example.com"],     # 发件人须含其一（小写子串匹配）；空=不限制
    "subject_keywords": [],            # 主题须含其一；空=不限制
    "max_bytes": 15728640,             # 单附件上限 15MB
    "download_links": True,            # 正文里的发票 PDF 下载链接自动下载打印（如德邦）
    # 轮询
    "poll_seconds": 60,
    "once": False,                     # True 时只跑一轮就退出（cron/调试用）
    "print_only_new": True,            # 首启不打印邮箱历史，只打印启动后新收到的
    # 默认预设（分类都不命中时）：null=拒绝打印
    "default_preset": "a5",
    # 两种发票预设（字段即打印 spec）
    "presets": {
        "a5": {"paper": "A5", "scale_mode": "fit", "collate": True,
               "duplex": "one-sided", "grayscale": False, "per_sheet": 1},
        "a4": {"paper": "A4", "scale_mode": "fit", "collate": True,
               "duplex": "two-sided-long-edge", "grayscale": False, "per_sheet": 1}
    },
    # 分类规则：顺序匹配，先比文件名再比正文关键词。
    # 第 1 条：德邦（dpdzfp@deppon.com.cn）——链接下载的发票文件名是 dzfp_*.pdf；
    #         德邦的 PDF（含运单明细）正文含「德邦」→ A5 单面。
    #         必须放在顺丰「运单明细→A4」之前，否则德邦运单明细会被误分到 A4。
    # 第 2 条：单独的「运单明细」类邮件（文件名或正文含运单明细）→ A4 双面。
    # 第 3 条：顺丰/电子发票 → A5；但若正文同时含运单明细/运费明细等，
    #         说明是「发票+明细」合在一起的那种 → 改用 A4 双面（detail_preset）。
    "classify": [
        {"preset": "a5", "filenames": ["dzfp"], "text": ["德邦"]},
        {"preset": "a4", "filenames": ["运单明细", "waybill"],
         "text": ["运单明细", "运费明细", "运输明细"]},
        {"preset": "a5", "filenames": ["顺丰", "电子发票", "invoice"],
         "text": ["电子发票", "发票号码", "税号"],
         "detail_text": ["运单明细", "运费明细", "运输明细", "明细清单", "货物明细"],
         "detail_preset": "a4"}
    ],
    # 关键词门禁：抽出的正文须含其一才算发票（空=不门禁）
    "gate_keywords": ["发票", "税号", "金额", "invoice", "waybill", "明细", "顺丰"],
    # 工具与外部
    "pdftotext": "pdftotext",          # 路径或 None 跳过文字提取
    "printer": "",                     # 强制指定队列；空=用网关默认
    "state_file": "invoice_mailer_seen.json",
    "fail_dir": "invoice_fail",
    # 邮件处理记录日志（JSONL，每行一条）：相对路径会解析到 state_file 同目录。
    "log_file": "invoice_mailer_log.jsonl",
    "log_keep": 1000,                   # 保留最近多少条，超出自动裁剪（防小盘写爆）
    "dry_run": False
}

# ----------------------------------------------------------------- 工具
def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (ts, msg), flush=True)


def _url(cfg, path):
    u = cfg["gateway"].rstrip("/") + path
    tok = cfg.get("token")
    if tok:
        u += ("&" if "?" in u else "?") + "t=" + urllib.parse.quote(tok, safe="")
    return u


def post_multipart(url, file_bytes, filename, token=None):
    """上传一个 PDF 到 /api/upload（multipart/form-data, 字段名 file）。"""
    boundary = "----invmail%d" % int(time.time() * 1000)
    body = io.BytesIO()
    body.write(("--" + boundary + "\r\n").encode())
    cd = 'Content-Disposition: form-data; name="file"; filename="%s"\r\n' % filename
    body.write(cd.encode())
    body.write(b"Content-Type: application/pdf\r\n\r\n")
    body.write(file_bytes)
    body.write(("\r\n--" + boundary + "--\r\n").encode())
    data = body.getvalue()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


# ----------------------------------------------------------------- 识别
def classify(filename, text, cfg):
    """按规则分类发票预设。

    命中规则后再做二级分流：若规则的 detail_text 命中正文，则改用
    detail_preset（用于顺丰电子发票两类——纯发票→A5、含运单明细→A4 双面）。
    """
    fn = (filename or "").lower()
    low = (text or "").lower()
    for rule in cfg.get("classify", []):
        hit = False
        for k in rule.get("filenames", []):
            if k.lower() in fn:
                hit = True
                break
        if not hit:
            for k in rule.get("text", []):
                if k.lower() in low:
                    hit = True
                    break
        if not hit:
            continue
        dt = rule.get("detail_text")
        if dt and any(k.lower() in low for k in dt):
            dp = rule.get("detail_preset")
            if dp:
                return dp
        return rule["preset"]
    return cfg.get("default_preset") or None


def gate_ok(text, cfg):
    kws = cfg.get("gate_keywords") or []
    if not kws:
        return True
    low = (text or "").lower()
    return any(k.lower() in low for k in kws)


_INV_NO_RE = [
    re.compile(r'发票号码[：:]\s*([A-Za-z0-9\-]{6,})'),
    re.compile(r'发票代码[：:]\s*([A-Za-z0-9\-]{6,})'),
    re.compile(r'No\.?\s*([A-Za-z0-9\-]{6,})', re.I),
    re.compile(r'号码[：:]\s*([A-Za-z0-9\-]{6,})'),
]


def extract_invoice_no(text):
    if not text:
        return ""
    for rx in _INV_NO_RE:
        m = rx.search(text)
        if m:
            return m.group(1)
    return ""


def extract_text(pdf_bytes, cfg):
    binp = cfg.get("pdftotext")
    if not binp:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(pdf_bytes)
        path = tf.name
    try:
        try:
            out = subprocess.run([binp, path, "-"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=60)
            return out.stdout.decode("utf-8", "replace")
        except FileNotFoundError:
            return ""
        except subprocess.TimeoutExpired:
            return ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ----------------------------------------------------------------- 网关
def submit(pdf_bytes, filename, preset_key, cfg):
    presets = cfg.get("presets", {})
    if preset_key not in presets:
        raise ValueError("未知预设: " + str(preset_key))
    spec = dict(presets[preset_key])
    if cfg.get("printer"):
        spec["printer"] = cfg["printer"]

    if cfg.get("dry_run"):
        log("  [dry-run] 将提交到网关 %s，预设=%s，文件名=%s"
            % (cfg["gateway"], preset_key, filename))
        return {"dry_run": True, "preset": preset_key}

    up = post_multipart(_url(cfg, "/api/upload"), pdf_bytes, filename)
    if up.get("error"):
        raise RuntimeError("上传失败: " + up.get("error"))
    job = up.get("id")
    pages = up.get("pages")
    log("  上传成功 作业=%s 页数=%s" % (job, pages))
    res = post_json(_url(cfg, "/api/print"), {"job": job, "spec": spec})
    if res.get("error"):
        raise RuntimeError("打印失败: " + res.get("error"))
    log("  已提交打印 任务号=%s" % res.get("job"))
    return res


# ----------------------------------------------------------------- 邮件
def _connect(cfg):
    im = cfg["imap"]
    # timeout：连接和后续 IMAP 命令的 socket 超时（Python 3.9+）。
    # 不设的话网络一旦半死（包被丢、连接被墙），轮询会永久挂起——
    # 进程不死、systemd 不会拉起，表现为「静默卡死再也不收邮件」。
    to = int(cfg.get("imap_timeout", 60) or 60)
    if im.get("ssl"):
        c = imaplib.IMAP4_SSL(im["host"], int(im.get("port", 993)), timeout=to)
    else:
        c = imaplib.IMAP4(im["host"], int(im.get("port", 143)), timeout=to)
    c.login(im["user"], im["password"])
    return c


def _hdr(val):
    """把邮件头取值安全转成 str。

    某些邮件（带复杂 RFC2047 编码头的）经 email 解析后，msg.get() 返回的是
    email.header.Header 对象而非 str，直接 .lower() 会抛
    'Header' object has no attribute 'lower'。这里统一收敛成 str。
    """
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    try:
        return str(make_header(decode_header(val)))
    except Exception:
        try:
            return str(val)
        except Exception:
            return ""


def sender_allowed(frm, cfg):
    wl = cfg.get("whitelist") or []
    if not wl:
        return True
    low = (frm or "").lower()
    return any(w.lower() in low for w in wl)


def subject_ok(subject, cfg):
    ks = cfg.get("subject_keywords") or []
    if not ks:
        return True
    low = (subject or "").lower()
    return any(k.lower() in low for k in ks)


def load_seen(path):
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(path, seen):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=0)


# ----------------------------------------------------------------- 记录日志
def _log_path(cfg):
    """解析日志文件路径：log_file 若为绝对路径直接用；相对路径则落到
    state_file 同目录（部署时 state_file 通常是 /var/spool/... 的绝对路径）。"""
    lp = cfg.get("log_file") or "invoice_mailer_log.jsonl"
    if os.path.isabs(lp):
        return lp
    sf = cfg.get("state_file") or "invoice_mailer_seen.json"
    if os.path.isabs(sf):
        return os.path.join(os.path.dirname(sf), lp)
    return lp


def append_log(rec, cfg):
    """追加一条 JSON 记录；超过 log_keep 时裁剪到最近 N 条（整文件重写一次）。"""
    try:
        p = _log_path(cfg)
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        keep = int(cfg.get("log_keep", 1000) or 1000)
        if keep > 0 and os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
                if len(lines) >= keep:
                    lines = lines[-(keep - 1):]
                    with open(p, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines) + "\n")
            except Exception:
                pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log("!! 写记录日志失败：%s" % e)


def _decode_hdr(val):
    """把可能带 RFC2047 编码的邮件头（=?UTF-8?B?...?=）解成明文。

    _hdr() 只兜底 Header 对象、对 str 原样返回；但网页展示需要明文，
    所以记录日志前再过一道真正的解码。对已是明文的输入是幂等的。
    """
    if not val:
        return ""
    try:
        return str(make_header(decode_header(str(val))))
    except Exception:
        return str(val)


def _write_record(cfg, frm, subject, fn, preset, job, pages, ok,
                  error="", invoice_no=""):
    """把**一个打印文件**的处理结果写进记录日志（供后台「打印记录」展示）。

    记录单位是文件而非邮件：只有真正提交打印（成功或失败）的附件才记。
    ts 存 Unix 时间戳（秒），页面用浏览器本地时区渲染——设备多为 UTC，
    直接存设备本地时间会比北京时间慢 8 小时。
    """
    rec = {
        "ts": int(time.time()),
        "file": fn,
        "preset": preset,
        "pages": pages,
        "job": job,
        "invoice_no": invoice_no or "",
        "ok": bool(ok),
        "error": error or "",
        "from": _decode_hdr(frm),
        "subject": _decode_hdr(subject),
        "dry_run": bool(cfg.get("dry_run")),
    }
    append_log(rec, cfg)


_URL_RE = re.compile(r'https?://[^\s\'"<>()\[\]{}]+', re.I)


def _body_text(msg):
    """汇总邮件正文的纯文本（text/plain 优先，只有 text/html 时去标签兜底）。

    用于从正文提取发票 PDF 下载链接（如德邦的链接式发票邮件）。
    """
    plains, htmls = [], []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            txt = payload.decode(charset, "replace")
        except Exception:
            continue
        (plains if ct == "text/plain" else htmls).append(txt)
    if plains:
        return "\n".join(plains)
    return re.sub(r"<[^>]+>", " ", "\n".join(htmls))


def _extract_pdf_urls(body):
    """从正文提取 .pdf 结尾的下载链接（去重、保序，容忍首尾引号/标点）。"""
    urls = []
    for m in _URL_RE.finditer(body or ""):
        u = m.group(0).rstrip(".,;，。；）)\"'")
        if u.split("?")[0].lower().endswith(".pdf") and u not in urls:
            urls.append(u)
    return urls


def _download(url, cfg):
    """下载正文里的发票 PDF；非 PDF 响应或超过 max_bytes 都拒绝。"""
    limit = int(cfg.get("max_bytes", 15728640))
    req = urllib.request.Request(
        url, headers={"User-Agent": "invoice-mailer/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        data = r.read(limit + 1)
    if "pdf" not in ctype and not data[:5].startswith(b"%PDF-"):
        raise ValueError("响应不是 PDF (%s)" % ctype.split(";")[0])
    if len(data) > limit:
        raise ValueError("文件超过大小上限 %d 字节" % limit)
    return data


def process_message(msg_bytes, cfg, seen):
    msg = email.message_from_bytes(msg_bytes)
    mid = _hdr(msg.get("Message-ID", "")).strip()
    frm = _hdr(msg.get("From", ""))
    subject = _hdr(msg.get("Subject", ""))
    if mid and mid in seen:
        return  # 已处理，跳过
    if not sender_allowed(frm, cfg):
        log("跳过（发件人不在白名单）: %s | %s" % (frm, subject))
        if mid:
            seen.add(mid)
        return
    if not subject_ok(subject, cfg):
        log("跳过（主题不含关键词）: %s" % subject)
        if mid:
            seen.add(mid)
        return

    failed = False
    for part in msg.walk():
        cd = part.get("Content-Disposition") or ""
        fn_raw = part.get_filename() or ""
        # 附件名常为 RFC2047 编码（如 =?UTF-8?B?...?=），不解码的话网关按扩展名
        # 取不到 .pdf，会误判「不支持的文件类型」而拒绝上传。这里先解码成明文。
        fn = str(make_header(decode_header(fn_raw))) if fn_raw else ""
        ctype = part.get_content_type()
        is_pdf = (fn.lower().endswith(".pdf")
                  or "pdf" in ctype.lower())
        if not is_pdf:
            continue
        payload = part.get_payload(decode=True) or b""
        size = len(payload)
        if size > int(cfg.get("max_bytes", 1 << 30)):
            log("跳过（附件过大 %d 字节）: %s" % (size, fn))
            continue
        text = extract_text(payload, cfg)
        preset = classify(fn, text, cfg)
        if not preset:
            log("跳过（无法分类，默认预设为 null）: %s" % fn)
            continue
        if not gate_ok(text, cfg):
            log("跳过（关键词门禁未通过，疑似非发票）: %s" % fn)
            continue
        inv_no = extract_invoice_no(text)
        log("发票命中：文件=%s 预设=%s 发票号=%s" % (fn, preset, inv_no or "-"))
        try:
            res = submit(payload, fn, preset, cfg)
            # 记录单位 = 打印文件（成功）
            _write_record(cfg, frm, subject, fn, preset,
                          res.get("job"), res.get("pages"), True,
                          invoice_no=inv_no or "")
        except Exception as e:
            log("!! 提交失败：%s" % e)
            _save_fail(cfg, fn, payload)
            # 记录单位 = 打印文件（失败，下轮重试成功后会再记一条成功）
            _write_record(cfg, frm, subject, fn, preset, "", None, False,
                          error=str(e), invoice_no=inv_no or "")
            failed = True
    # 正文链接式发票（如德邦：发票不在附件，正文给出 PDF 下载地址）
    if cfg.get("download_links", True):
        for url in _extract_pdf_urls(_body_text(msg)):
            fn = urllib.parse.unquote(
                url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]) or "download.pdf"
            if not fn.lower().endswith(".pdf"):
                fn += ".pdf"
            log("正文 PDF 链接 -> %s" % fn)
            try:
                payload = _download(url, cfg)
            except Exception as e:
                log("!! 链接下载失败：%s" % e)
                _write_record(cfg, frm, subject, fn, None, "", None, False,
                              error="下载失败: %s" % e)
                failed = True   # 下轮重试（链接失效会一直失败，可在后台看到）
                continue
            text = extract_text(payload, cfg)
            preset = classify(fn, text, cfg)
            if not preset:
                log("跳过（链接文件无法分类）: %s" % fn)
                continue
            if not gate_ok(text, cfg):
                log("跳过（链接文件关键词门禁未通过）: %s" % fn)
                continue
            inv_no = extract_invoice_no(text)
            log("发票命中（正文链接）：文件=%s 预设=%s 发票号=%s"
                % (fn, preset, inv_no or "-"))
            try:
                res = submit(payload, fn, preset, cfg)
                _write_record(cfg, frm, subject, fn, preset,
                              res.get("job"), res.get("pages"), True,
                              invoice_no=inv_no or "")
            except Exception as e:
                log("!! 提交失败：%s" % e)
                _save_fail(cfg, fn, payload)
                _write_record(cfg, frm, subject, fn, preset, "", None, False,
                              error=str(e), invoice_no=inv_no or "")
                failed = True
    # 只要有一个附件提交失败，就不当作已处理，留待下一轮轮询重试
    # （已成功打印的附件会在重试时再打一次，但绝不漏打失败的附件）
    if mid and not failed:
        seen.add(mid)


def _save_fail(cfg, fn, payload):
    d = cfg.get("fail_dir", "invoice_fail")
    try:
        os.makedirs(d, exist_ok=True)
        safe = re.sub(r'[^A-Za-z0-9._\-]', '_', fn or "invoice.pdf")
        with open(os.path.join(d, safe), "wb") as f:
            f.write(payload)
        log("  失败附件已存至 %s/%s" % (d, safe))
    except Exception as e:
        log("  !! 保存失败附件出错: %s" % e)


def warmup(cfg, seen):
    """首启预热：把邮箱里现有邮件全部标记为已见，避免一启动就打印一堆历史。

    只在「状态文件不存在」时调用（见 main）。状态文件存在说明之前跑过，
    那时未打印的邮件（停机期间到达）应照常打印，故不做预热。
    """
    try:
        c = _connect(cfg)
    except Exception as e:
        log("!! 首启预热连接失败，将照常处理全部邮件: %s" % e)
        return
    try:
        c.select(cfg.get("mailbox", "INBOX"), readonly=True)
        typ, data = c.search(None, "ALL")
        if typ == "OK" and data and data[0]:
            for num in data[0].split():
                typ2, msg_data = c.fetch(num, "(RFC822.HEADER)")
                if typ2 != "OK":
                    continue
                raw = None
                for blk in msg_data:
                    if isinstance(blk, tuple):
                        raw = blk[1]
                if raw:
                    mid = _hdr(email.message_from_bytes(raw).get("Message-ID", "")).strip()
                    if mid:
                        seen.add(mid)
    finally:
        try:
            c.close()
        except Exception:
            pass
        c.logout()


def poll_once(cfg, seen):
    c = _connect(cfg)
    try:
        c.select(cfg.get("mailbox", "INBOX"), readonly=True)
        typ, data = c.search(None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return
        for num in data[0].split():
            typ, msg_data = c.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            raw = None
            for blk in msg_data:
                if isinstance(blk, tuple):
                    raw = blk[1]
            if raw:
                process_message(raw, cfg, seen)
    finally:
        try:
            c.close()
        except Exception:
            pass
        c.logout()


# ----------------------------------------------------------------- 测试
def self_test(cfg):
    samples = [
        ("顺丰电子发票.pdf", "电子发票 发票号码: 12345678 税号 91110000"),
        ("顺丰电子发票（含明细）.pdf", "电子发票 发票号码: 12345678 运费明细 收件人 张三"),
        ("2026发票运单明细.pdf", "运单明细 收件人 张三"),
        ("invoice-2026.pdf", "Invoice No. AB123456 金额 99.00"),
        ("广告.pdf", "限时折扣 全场五折 速来"),
        ("未知.pdf", "一些无关文本"),
    ]
    print("%-26s %-6s %-5s %s" % ("文件名", "预设", "门禁", "发票号"))
    print("-" * 70)
    for fn, text in samples:
        preset = classify(fn, text, cfg)
        ok = gate_ok(text, cfg)
        no = extract_invoice_no(text)
        print("%-26s %-6s %-5s %s" % (fn, str(preset), "Y" if ok else "N", no))
    print("-" * 70)
    print("（default_preset=%s，未命中规则时回退到此；门禁由 gate_keywords 控制）"
          % cfg.get("default_preset"))


def classify_file(path, cfg):
    with open(path, "rb") as f:
        payload = f.read()
    text = extract_text(payload, cfg)
    preset = classify(os.path.basename(path), text, cfg)
    ok = gate_ok(text, cfg)
    print("文件: %s" % path)
    print("  PDF 字节 : %d" % len(payload))
    print("  正文长度 : %d 字符" % len(text))
    print("  分类预设 : %s" % preset)
    print("  门禁通过 : %s" % ("Y" if ok else "N"))
    print("  发票号   : %s" % (extract_invoice_no(text) or "-"))


def print_unit(cfg):
    svc = "invoice-mailer"
    unit = """[Unit]
Description=Invoice Mailer - 邮件收件自动打印发票
After=network-online.target

[Service]
Type=simple
WorkingDirectory={wd}
ExecStart={py} {script} --config {cfg}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier={svc}

[Install]
WantedBy=multi-user.target
""".format(wd=os.getcwd(), py=sys.executable,
           script=os.path.abspath(__file__),
           cfg=os.path.abspath(cfg.get("_config_path", "invoice_mailer.json")),
           svc=svc)
    print(unit)


# ----------------------------------------------------------------- 入口
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        user = json.load(f)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update(user)
    cfg["_config_path"] = path
    # 合并嵌套 dict（presets/classify 用用户整体覆盖，不逐键 merge）
    for k in ("presets", "classify", "imap"):
        if k in user:
            cfg[k] = user[k]
    return cfg


def main():
    ap = argparse.ArgumentParser(description="发票邮件自动打印守护进程")
    ap.add_argument("--config", default="invoice_mailer.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true", help="只跑一轮后退出")
    ap.add_argument("--self-test", action="store_true", help="演示分类/门禁逻辑")
    ap.add_argument("--classify-file", metavar="PATH", help="对本地 PDF 试分类")
    ap.add_argument("--print-unit", action="store_true", help="输出 systemd unit 到 stdout")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.dry_run:
        cfg["dry_run"] = True
    if args.once:
        cfg["once"] = True

    if args.self_test:
        self_test(cfg)
        return
    if args.classify_file:
        classify_file(args.classify_file, cfg)
        return
    if args.print_unit:
        print_unit(cfg)
        return

    state_file = cfg.get("state_file", "invoice_mailer_seen.json")
    seen = load_seen(state_file)
    # 首启（状态文件不存在）且开启 print_only_new：先把邮箱现有邮件全部标记为已见，
    # 之后只打印新收到的，避免开机就把整箱历史打出来。
    if cfg.get("print_only_new", True) and not os.path.exists(state_file):
        warmup(cfg, seen)
        save_seen(state_file, seen)
        log("首启预热完成：邮箱现有 %d 封邮件已标记为已读，仅打印此后新收到的"
            % len(seen))
    log("启动 invoice-mailer（gateway=%s, dry_run=%s）"
        % (cfg["gateway"], cfg.get("dry_run")))
    try:
        if cfg.get("once"):
            poll_once(cfg, seen)
            save_seen(cfg["state_file"], seen)
        else:
            while True:
                try:
                    poll_once(cfg, seen)
                    save_seen(cfg["state_file"], seen)
                except Exception as e:
                    log("!! 本轮异常：%s" % e)
                time.sleep(int(cfg.get("poll_seconds", 60)))
    except KeyboardInterrupt:
        log("收到中断，退出")


if __name__ == "__main__":
    main()
