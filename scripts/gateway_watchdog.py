#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打印网关看门狗（设备端常驻，systemd 拉起，Restart=always 自保活）。

目标：dev-10 那块 ARM 板内存只有 ~1G，偶尔会「卡死」——
可以是网关进程挂起（线程死锁 / 内存泄漏涨满），也可以是 CUPS 调度器掉线。
主单元 print-gateway 只有 Restart=on-failure，只重启「崩溃」，抓不住「活着但没响应」，
所以这里做进程外的健康检查。

三道防线：
  1. 网关 HTTP 健康：连 127.0.0.1:8080 超时 / 非 200 连续 N 次 → 重启 print-gateway
  2. CUPS 调度器：lpstat -r 不是 "scheduler is running" → 重启 cups
  3. 内存看门狗：可用内存低于阈值连续 N 次 → 重启 print-gateway（回收泄漏内存）

护栏：
  - 同一服务两次重启间隔不小于 COOLDOWN_SEC，避免重启风暴
  - 主循环包 try，任何异常只记日志不退出；systemd 的 Restart=always 兜底
  - 日志同时写 journal（stdout）与 /var/log/gateway-watchdog.log（环形保留）

可调参数集中在文件顶部。
"""
import datetime
import os
import subprocess
import sys
import time
import urllib.request

# ----------------------------- 可调参数
GATEWAY_HTTP = "http://127.0.0.1:8080/"
CHECK_INTERVAL = 30          # 每轮检查间隔（秒）
HTTP_TIMEOUT = 8             # 单次 HTTP 超时（秒）
HTTP_FAILS_TO_RESTART = 3    # 连续几次无响应才重启
MEM_AVAIL_MB_THRESHOLD = 40  # 可用内存低于此值（MB）视为危险
MEM_FAILS_TO_RESTART = 3     # 连续几次低内存才重启
COOLDOWN_SEC = 180           # 同一服务两次重启的最小间隔
LOG_PATH = "/var/log/gateway-watchdog.log"
MAX_LOG_BYTES = 200_000      # 日志超过此值后只保留尾部一半（环形）
# -----------------------------

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (ts, msg)
    print(line, flush=True)
    try:
        if (os.path.exists(LOG_PATH)
                and os.path.getsize(LOG_PATH) > MAX_LOG_BYTES):
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.write(data[-MAX_LOG_BYTES // 2:])
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _stamp_path(service):
    return "/run/gateway-watchdog-%s" % service


def last_restart(service):
    try:
        with open(_stamp_path(service)) as f:
            return float(f.read().strip() or 0)
    except Exception:
        return 0.0


def mark_restart(service):
    try:
        with open(_stamp_path(service), "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def can_restart(service):
    return (time.time() - last_restart(service)) >= COOLDOWN_SEC


def restart_service(service):
    if not can_restart(service):
        log("  冷却中，跳过重启 %s" % service)
        return False
    log(">> 重启服务: %s" % service)
    try:
        subprocess.run(["systemctl", "restart", service], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        mark_restart(service)
        log("  %s 重启完成" % service)
        return True
    except Exception as e:
        log("  !! 重启 %s 失败: %s" % (service, e))
        return False


def http_ok():
    try:
        urllib.request.urlopen(GATEWAY_HTTP, timeout=HTTP_TIMEOUT)
        return True
    except Exception:
        return False


def cups_ok():
    try:
        r = subprocess.run(["lpstat", "-r"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=10)
        return "scheduler is running" in r.stdout.decode("utf-8", "replace")
    except Exception:
        return False


def mem_avail_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


def main():
    log("看门狗启动 (interval=%ds http_timeout=%ds mem_th=%dMB cooldown=%ds)"
        % (CHECK_INTERVAL, HTTP_TIMEOUT, MEM_AVAIL_MB_THRESHOLD, COOLDOWN_SEC))
    http_fails = 0
    mem_fails = 0
    while True:
        try:
            # 1. 网关 HTTP 健康
            if http_ok():
                if http_fails:
                    log("网关 HTTP 恢复")
                http_fails = 0
            else:
                http_fails += 1
                log("网关 HTTP 无响应 (%d/%d)"
                    % (http_fails, HTTP_FAILS_TO_RESTART))
                if http_fails >= HTTP_FAILS_TO_RESTART:
                    if restart_service("print-gateway"):
                        http_fails = 0

            # 2. CUPS 调度器
            if not cups_ok():
                log("CUPS 调度器未运行，尝试重启 cups")
                restart_service("cups")

            # 3. 内存看门狗
            mb = mem_avail_mb()
            if mb is not None and mb < MEM_AVAIL_MB_THRESHOLD:
                mem_fails += 1
                log("可用内存偏低 %dMB (%d/%d)"
                    % (mb, mem_fails, MEM_FAILS_TO_RESTART))
                if mem_fails >= MEM_FAILS_TO_RESTART:
                    if restart_service("print-gateway"):
                        mem_fails = 0
            else:
                mem_fails = 0
        except Exception as e:
            log("!! 看门狗本轮异常: %s" % e)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("看门狗退出")
