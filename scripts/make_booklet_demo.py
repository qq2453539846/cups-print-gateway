#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成「小册子图标界面」的离线演示页 —— 给不拿手机、也不想上传文件的人看界面用。

    python3 make_booklet_demo.py                          # 默认写 ../outputs/booklet_ui_demo.html
    python3 make_booklet_demo.py --out demo.html --max 48

做法：把 print_gateway.py 里那份 PAGE 的 **CSS、小册子那几行控件、bk* 前端函数**
原样抠出来拼成一个单页，再配一张预先算好的「页数 → 配对」表。所以演示页里
看到的图标、文案、点击行为就是设备上那一份，不存在「演示和实际不一样」。

配对页号不重算 —— 表是 `pg_layout.booklet_sides()` 在生成阶段跑出来的，和实印、
和 `/api/upload` 返回的 booklet 摘要同源。前端仍然只做「挑正/背面」+「右装订对调」，
跟前端代码里的约定保持一致。
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SRC = os.path.join(HERE, "print_gateway.py")

DEMO_CSS_EXTRA = """
body{padding:0}
.demo-note{background:#eef3fe;border:1px solid #d6e2fb;color:#2b6de5;
border-radius:8px;padding:9px 12px;font-size:13px;margin-bottom:12px;line-height:1.6}
.demo-note b{color:#1f2329}
.demo-slide{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.demo-slide label{flex:0 0 84px;color:var(--muted);font-size:13px}
.demo-slide input[type=range]{flex:1;min-width:0;accent-color:var(--accent)}
.demo-slide output{flex:0 0 62px;text-align:right;font-size:13px;color:var(--text)}
.demo-hint{font-size:12px;color:var(--muted);margin-top:8px;line-height:1.7}
"""


def slice_between(text, start_mark, end_mark, what):
    i = text.find(start_mark)
    if i < 0:
        sys.exit("在 print_gateway.py 里找不到「%s」的起点：%s" % (what, start_mark))
    j = text.find(end_mark, i + len(start_mark))
    if j < 0:
        sys.exit("在 print_gateway.py 里找不到「%s」的终点：%s" % (what, end_mark))
    return text[i:j]


def build_table(max_pages):
    """
    预先算好 1..max_pages 的配对。

    直接调用网关自己的 booklet_summary()，而不是在这里重写一遍补空白/算张数 ——
    凡是「重写一遍」的地方迟早会和后端跑偏，这个坑在前面已经踩过。
    """
    import print_gateway                                            # noqa: E402

    return {n: print_gateway.booklet_summary(n)
            for n in range(1, max_pages + 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "..", "outputs",
                                                  "booklet_ui_demo.html"))
    ap.add_argument("--max", type=int, default=32, help="滑杆最大页数")
    args = ap.parse_args()

    src = open(SRC, encoding="utf-8").read()

    m = re.search(r"<style>(.*?)</style>", src, re.S)
    if not m:
        sys.exit("抠不到 <style>")
    css = m.group(1)

    checkbox = slice_between(src, '<div class="row"><label>小册子</label>',
                             "</div>", "小册子勾选行")
    checkbox += "</div>"

    markup = slice_between(src, '<input type="hidden" id="bookletBinding"',
                           '<div class="row"><label>双面</label>', "小册子控件块")

    js = slice_between(src, "// ---- 小册子图标示意", "function syncUI(){",
                       "小册子前端函数")

    table = build_table(args.max)

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小册子图标界面 · 离线演示</title>
<style>{css}{extra}</style>
</head>
<body>
<header>
  <h1>小册子图标界面 · 离线演示</h1>
  <span class="badge">从 print_gateway.py 原样抠出</span>
</header>

<div class="wrap">
  <div class="left" style="flex:1 1 100%;max-width:660px">
    <div class="demo-note">
      这一页的 <b>样式、图标、点击行为</b> 都是直接从 <code>print_gateway.py</code>
      里抠出来的真代码，和设备上那一份一致。<br>
      只有「文档页数」是滑杆模拟的 —— 配对页号来自 <code>pg_layout.booklet_sides()</code>
      预先算好的表，与实印、与 <code>/api/upload</code> 返回的 booklet 摘要同源。
    </div>

    <div class="demo-slide">
      <label>文档页数</label>
      <input type="range" id="demoPages" min="1" max="{maxp}" value="10">
      <output id="demoLabel">10 页</output>
    </div>

    <div class="card">
      <h2>打印设置<span class="sub">小册子那几行</span></h2>
      <div class="row" style="margin-bottom:12px">
        <label>小册子</label>
        {checkbox}
      </div>
      {markup}
    </div>

    <div class="demo-hint">
      图标约定：<b>实线纸</b> = 会印的那一面，<b>虚线纸</b> = 不印，<b>橙色竖条</b> = 订口那一侧。<br>
      「示意」里的页号是这一张纸这一面实际放哪几页；右装订只是把左右对调，配对本身不变。
    </div>
  </div>
</div>

<script>
var JOB = null, TIMER = null;
var $ = function(s){{ return document.querySelector(s); }};
var BK_TABLE = {table};
{js}
/* ---- 演示用挂钩：真实页面里这几行由上传/预览回调驱动 ---- */
var __slider = $('#demoPages');
function demoLoad(){{
  var n = +__slider.value;
  JOB = {{id:'demo', filename:'演示.pdf', pages:n, files:1, booklet:BK_TABLE[n]}};
  $('#demoLabel').textContent = n + ' 页';
  bkPaint();
}}
$('#booklet').addEventListener('change', bkPaint);
$('#bookletBinding').addEventListener('change', bkPaint);
$('#bookletSubset').addEventListener('change', bkPaint);
[].forEach.call(document.querySelectorAll('.bkbtn'), function(b){{
  b.addEventListener('click', function(){{
    bkPick(b.getAttribute('data-for'), b.getAttribute('data-val'));
  }});
}});
__slider.addEventListener('input', demoLoad);
$('#booklet').checked = true;
demoLoad();
</script>
</body>
</html>
""".format(css=css, extra=DEMO_CSS_EXTRA, maxp=args.max,
           checkbox=checkbox, markup=markup,
           table=json.dumps(table, ensure_ascii=False, separators=(",", ":")),
           js=js)

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    print("演示页写到：%s（%.1f KB，页数滑杆 1-%d）"
          % (out, len(html.encode("utf-8")) / 1024.0, args.max))
    return 0


if __name__ == "__main__":
    sys.exit(main())
