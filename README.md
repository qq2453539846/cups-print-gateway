# cups-print-gateway

自托管 CUPS 打印网关：手机扫码上传 PDF/图片直接打印，配套**邮件发票自动打印**守护进程（顺丰/德邦等电商电子发票收到即打，A5/A4 自动分流），纯 Python 标准库实现，无第三方依赖。

灵感来自商业打印方案（WPS PDF 专业版等）的「发票专格打印」体验，但为**干净实现**——未使用、未反编译任何闭源代码。

## 功能总览

### 扫码打印网关（print_gateway.py）
- 手机扫码 → 网页上传 PDF/图片 → 自动版式处理 → CUPS 打印
- 伪打印 preview（矢量渲染，所见即所得）、小册子/拼版、贴纸/装饰页（pg_sticker/pg_decor）
- Web 后台 `/admin`（口令保护）：打印记录、邮件自动打印配置、DDNS 管理
- HTTPS（自签证书）+ 明文双端口，QR 码自动生成
- 内置看门狗（gateway_watchdog.py）、任务 6 小时自动清理、内存护栏

### 邮件发票自动打印（invoice_mailer.py）
- IMAP 轮询专用邮箱 → 发件人白名单 → PDF 附件提取 → 分类打印
- **正文链接式发票**：德邦等把发票以下载链接（`dzfp_*.pdf`）放正文，自动下载后走同一打印流程
- **按内容分类**（不只靠文件名，顺丰两类文件名相同）：
  - 顺丰电子发票（纯发票）→ A5 单面
  - 顺丰电子发票（含运单/运费明细）→ A4 双面
  - 德邦（运单明细/链接下载发票）→ A5 单面
- 关键词门禁（pdftotext 抽文字确认识发票才打）、发票号提取
- 只打新收到的（首启预热、停机期间到达的照常打）、失败自动重试
- 每个打印文件一条结构化记录（JSONL），后台页面展示（北京时间渲染、60 秒自动刷新）
- 全链路超时保护：IMAP 60s / PDF 下载 30s / 网关提交 120s / pdftotext 60s

### DDNS（pg_ddns.py / pg_dns.py）
- 公网 IP 探测 + DNSPod（旧版 Token / 腾讯云 TC3）自动更新 A 记录，systemd timer 周期触发

## 目录结构

```
scripts/
  print_gateway.py     # 主网关（HTTP 服务、上传、打印、/admin 分发）
  pg_engine.py         # CUPS 引擎：CUPS 调用、版式处理、paper/duplex/拼版
  pg_layout.py         # 版式计算（A5/A4、缩放、双面、N-up）
  pg_admin.py          # /admin 后台页面 + 配置读写/掩码/测试 helper
  pg_ddns.py           # DDNS 入口（--mode ddns/status）
  pg_dns.py            # DNS 提供商客户端（DNSPod Token / 腾讯云 TC3）
  pg_decor.py          # 装饰页/水印
  pg_sticker.py        # 贴纸页（含 QR 码生成）
  gen_qr.py            # 独立 QR 码生成器（纯标准库）
  invoice_mailer.py    # 邮件发票自动打印守护进程
  gateway_watchdog.py  # 看门狗：HTTP 健康检查 + CUPS 检查 + 内存看门狗
  fix_a5_tray.py       # 修厂商 PPD 无 Auto 纸盒导致 A5 不进 A5 盒的老坑
  setup_printer.py     # CUPS 队列初始化/PPD 安装辅助
  sync_skill.py        # 与本地工作副本同步的小工具
  make_testfiles.py    # 生成测试用 PDF
  test_*.py            # 单元/接口测试（unittest，覆盖网关/引擎/后台/DNS/QR/贴纸）
  verify_*.py          # 端到端验证脚本
systemd/               # 单元文件示例（print-gateway / invoice-mailer / watchdog / ddns timer）
invoice_mailer.json.example  # 邮件自动打印配置模板
```

## 环境要求

- Linux（在 Armbian/Debian 上长期运行）+ CUPS + 一台已配置的打印机队列
- Python 3.9+（用了 `imaplib.IMAP4(timeout=)`，建议 3.11+）
- poppler-utils（`pdftotext` / `pdftoppm` / `pdfinfo` / `pdfunite`）
- （可选）ghostscript

## 快速开始

### 1. 打印网关

```bash
sudo mkdir -p /opt/print-gateway && sudo cp scripts/*.py /opt/print-gateway/
# 后台口令
echo 'ADMIN_TOKEN=换成强随机串' | sudo tee /etc/print-gateway/gateway.env
sudo cp systemd/print-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now print-gateway
# 验证
curl http://127.0.0.1:8080/healthz
```

浏览器打开 `http://<设备IP>:8080` 扫码上传即可打印；后台在 `/admin`（输入 ADMIN_TOKEN 登录）。

### 2. 邮件发票自动打印

```bash
# 准备一个专用收件邮箱（163/QQ 等需开 IMAP 并用「授权码」登录）
sudo cp invoice_mailer.json.example /etc/print-gateway/invoice_mailer.json
sudo vim /etc/print-gateway/invoice_mailer.json   # 填 imap 账号/授权码/白名单
sudo cp systemd/invoice-mailer.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now invoice-mailer

# 先 dry-run 观察分类/门禁（不出纸）
python3 invoice_mailer.py --config /etc/print-gateway/invoice_mailer.json --dry-run --once
# 单测分类逻辑
python3 invoice_mailer.py --self-test
python3 invoice_mailer.py --classify-file 某发票.pdf
```

也可以全部在 `/admin` 后台「发票邮件自动打印」卡片里配置（服务商预设、授权码掩码保存、测试连接、启用/停用）。

### 3. 看门狗（低内存设备建议）

```bash
sudo cp systemd/gateway-watchdog.service /etc/systemd/system/
sudo systemctl enable --now gateway-watchdog
```

## 分类规则怎么写

`invoice_mailer.json` 的 `classify` 是**顺序匹配**规则表，每条按「文件名关键词 → 正文关键词」命中，命中后可用 `detail_text`/`detail_preset` 做二级分流：

```jsonc
"classify": [
  // 必须放在「运单明细→A4」之前：德邦的 PDF 正文含「德邦」，
  // 否则德邦运单明细会被误分到 A4
  {"preset": "a5", "filenames": ["dzfp"], "text": ["德邦"]},
  {"preset": "a4", "filenames": ["运单明细", "waybill"],
   "text": ["运单明细", "运费明细", "运输明细"]},
  {"preset": "a5", "filenames": ["顺丰", "电子发票", "invoice"],
   "text": ["电子发票", "发票号码", "税号"],
   "detail_text": ["运单明细", "运费明细", "运输明细", "明细清单", "货物明细"],
   "detail_preset": "a4"}
]
```

`presets` 里每个键就是一份打印 spec（paper / duplex / scale_mode / collate / grayscale / per_sheet），`default_preset` 兜底（设为 `null` 则未命中一律不打）。

## 安全须知

- **白名单必配**：`whitelist` 是发件人小写子串匹配，不配等于任何人可触发出纸
- 关键词门禁 `gate_keywords` 再拦一层（附件正文不含发票特征词不打）
- `/admin` 与网关 API 均有口令保护；TLS 端口建议只在内网/反代后暴露
- IMAP 授权码、DNSPod 密钥只存设备本地配置文件；后台回显一律掩码，掩码值回传不会覆盖真实密钥
- 邮件记录日志默认保留最近 1000 条，自动裁剪

## 测试

```bash
cd scripts
python3 -m unittest test_pg_engine test_print_gateway test_pg_admin \
    test_pg_dns test_pg_ddns test_gen_qr test_pg_sticker -v
```

## 已知适配笔记

- 部分 Kyocera 等厂商 PPD 写死 `*DefaultInputSlot` 且无 `Auto`，A5 作业不会进 A5 纸盒 → 跑 `fix_a5_tray.py`（补 PPD Auto 定义 + `lpadmin -o InputSlot=Auto`，可选 SNMP 纸盒核实）
- 低内存板子（<1GB）建议保留 watchdog + systemd `MemoryMax` 护栏，并给 DDNS oneshot 单元加 `TimeoutStartSec`

## License

[MIT](LICENSE)
