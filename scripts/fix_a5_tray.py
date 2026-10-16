#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复 A5 / A3 等非默认尺寸发票的「自动选纸盒」问题（仅设备端 root 运行）。

现象：厂商 PPD 往往写死 *DefaultInputSlot 到某个具体纸盒，且常常没有 Auto
选项。于是 A5 作业被硬指定去默认纸盒，而默认盒里装的是 A4 —— 200 页发票会
全打错盒却仍显示「成功」，极难发现（见 skill pitfalls 第 25 条）。

修复步骤：
  1. 备份 PPD（.bak，仅首次）
  2. 若 PPD 缺 Auto 纸盒定义，补一条  *InputSlot Auto/Auto Select:
  3. 将 *DefaultInputSlot 设为 Auto
  4. lpadmin -p Q -o InputSlot=Auto   （队列级强制 Auto）
  5. lpoptions 校验当前值带 *
  6. （可选 --snmp-host）snmp_tray 核实各纸盒装的纸含 A5

注意：本脚本只动「纸盒选择」，不碰驱动其它行为；PPD 写回后权限保持 0644。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

PPD_DIR = "/etc/cups/ppd"

# Auto 纸盒的 PostScript 调用：推迟介质选择，让 CUPS 按作业 media 自动挑盒。
AUTO_DEF = ('*InputSlot Auto/Auto Select: '
            '"<< /ManualFeed false /DeferredMediaSelection true >> setpagedevice"\n')
DEFAULT_LINE = '*DefaultInputSlot: Auto\n'


def run(cmd, check=True):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = p.stdout.decode('utf-8', 'replace')
    err = p.stderr.decode('utf-8', 'replace')
    if check and p.returncode != 0:
        raise RuntimeError("命令失败 %s\n%s\n%s" % (' '.join(cmd), out, err))
    return p.returncode, out, err


def list_queues():
    rc, out, _ = run(['lpstat', '-p'], check=False)
    return re.findall(r'^printer (\S+)', out, re.M)


def patch_ppd(queue, dry_run, force):
    path = os.path.join(PPD_DIR, queue + '.ppd')
    if not os.path.exists(path):
        raise RuntimeError('找不到 PPD: ' + path)
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    text = ''.join(lines)

    has_auto_def = bool(re.search(r'^\*InputSlot\s+Auto/', text, re.M))
    has_default_auto = bool(re.search(r'^\*DefaultInputSlot:\s*Auto\s*$', text, re.M))

    if has_auto_def and has_default_auto and not force:
        print('  PPD 已是 Auto 纸盒，无需修改（如需重设请加 --force）')
        return False

    new_lines = list(lines)
    if not has_auto_def:
        inserted = False
        # 插在 *CloseUI: *InputSlot 之前，确保落在 InputSlot 选项组内
        for i in range(len(new_lines) - 1, -1, -1):
            if new_lines[i].startswith('*CloseUI:') and 'InputSlot' in new_lines[i]:
                new_lines.insert(i, AUTO_DEF)
                inserted = True
                break
        if not inserted:
            for i, ln in enumerate(new_lines):
                if ln.startswith('*DefaultInputSlot'):
                    new_lines.insert(i, AUTO_DEF)
                    inserted = True
                    break
        if not inserted:
            new_lines.append(AUTO_DEF)
        print('  + 已补 *InputSlot Auto/Auto Select 定义')
    else:
        print('  = *InputSlot Auto 定义已存在')

    replaced = False
    for i, ln in enumerate(new_lines):
        if ln.startswith('*DefaultInputSlot'):
            new_lines[i] = DEFAULT_LINE
            replaced = True
            break
    if not replaced:
        new_lines.append(DEFAULT_LINE)
    print('  + 已设 *DefaultInputSlot: Auto')

    if dry_run:
        print('  [dry-run] 不写回 PPD')
        return True

    bak = path + '.bak'
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        print('  已备份 -> ' + bak)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.writelines(new_lines)
    os.chmod(path, 0o644)          # cupsd 需要能读
    print('  已写回 PPD')
    return True


def set_inputslot_auto(queue):
    rc, out, err = run(['lpadmin', '-p', queue, '-o', 'InputSlot=Auto'], check=False)
    if rc != 0:
        print('  !! lpadmin 失败: ' + err.strip())
        return False
    print('  lpadmin -o InputSlot=Auto 成功')
    return True


def verify(queue):
    rc, out, _ = run(['lpoptions', '-p', queue, '-l'], check=False)
    # lpoptions -l 输出形如： InputSlot/Media Source: *Auto PF730A PF730B ...
    # 选项行以选项名开头，被 * 前缀标记的那个选择才是当前默认值。
    line = ""
    for ln in out.splitlines():
        if ln.startswith("InputSlot") or "/InputSlot" in ln:
            line = ln
            break
    if not line:
        print('  !! 未找到 InputSlot 选项，请检查 PPD')
        return False
    cur = re.search(r'\*(\S+)', line)
    print('  lpoptions 当前 InputSlot: ' + (cur.group(1) if cur else '?'))
    if re.search(r'\*Auto\b', line):
        print('  OK：Auto 已设为默认（*Auto 存在）')
        return True
    print('  !! 当前默认不是 Auto，请检查 PPD')
    return False


def snmp_check(host, community):
    try:
        import snmp_tray
    except ImportError:
        print('  [跳过] 无法 import snmp_tray（需与本脚本同目录）')
        return
    base = '1.3.6.1.2.1.43.8.2.1.'
    try:
        rows = snmp_tray.get(host, [base + k for k in ('3', '9', '13')])
    except Exception as e:
        print('  [跳过] SNMP 读取失败: %s' % e)
        return
    print('  纸盒纸张（SNMP）：')
    found_a5 = False
    for oid, val in rows:
        idx = oid.rsplit('.', 1)[-1]
        col = oid.rsplit('.', 2)[-2]
        print('    盒 %s 列%s: %s' % (idx, col, val))
        if col == '9' and 'A5' in str(val):
            found_a5 = True
    if found_a5:
        print('  OK：存在装有 A5 的纸盒，Auto 会去那里取')
    else:
        print('  !! 未检测到装有 A5 的纸盒，请先在对应纸盒放入 A5 纸')


def main():
    ap = argparse.ArgumentParser(description='修复 A5 发票的自动选纸盒')
    ap.add_argument('--printer', default='', help='队列名；留空则用 CUPS 默认队列')
    ap.add_argument('--snmp-host', default='', help='打印机 SNMP 地址，核实纸盒里的纸')
    ap.add_argument('--snmp-community', default='public')
    ap.add_argument('--dry-run', action='store_true', help='只分析、不写回 PPD')
    ap.add_argument('--force', action='store_true', help='即便已 Auto 也重写')
    args = ap.parse_args()

    queues = list_queues()
    if not queues:
        print('!! 未发现任何 CUPS 队列')
        sys.exit(1)
    queue = args.printer
    if not queue:
        rc, out, _ = run(['lpstat', '-d'], check=False)
        m = re.search(r'system default destination:\s*(\S+)', out)
        queue = m.group(1) if m else queues[0]
    print('>> 目标队列: ' + queue)

    print('>> 修补 PPD')
    patch_ppd(queue, args.dry_run, args.force)
    if not args.dry_run:
        set_inputslot_auto(queue)
        print('>> 校验')
        verify(queue)
    if args.snmp_host:
        print('>> SNMP 纸盒核实')
        snmp_check(args.snmp_host, args.snmp_community)
    print('>> 完成。建议随后真实出一张 A5 验证是否去了 A5 纸盒。')


if __name__ == '__main__':
    main()
