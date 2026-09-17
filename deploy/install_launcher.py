#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「漂浮聊天球」注入到 Zabbix 前端页面（在 Zabbix 前端所在机器上以 root 运行）。

它只做一件事：在 Zabbix 站点的 nginx 配置（默认 `/etc/nginx/conf.d/zabbix.conf`，可用 `--config` 指定）的 PHP location 里加 3 行 sub_filter，
把 `<script src="/ai/launcher.js" defer></script>` 注入到每个 HTML 页面的 `</body>` 前。
真正的显示逻辑全在 launcher.js 里（只在 dashboard 页面且已登录时才创建小球），
因此这里不动 Zabbix 自身的任何文件——`apt upgrade zabbix-frontend-php` 不会覆盖本改动。

用法：
    sudo python3 install_launcher.py              # 安装（幂等，可重复执行）
    sudo python3 install_launcher.py --status     # 只看当前状态
    sudo python3 install_launcher.py --uninstall  # 卸载（精确移除本脚本加的行）

任何一步失败都会自动恢复备份并退出非 0。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

DEFAULT_CONFIG = "/etc/nginx/conf.d/zabbix.conf"
MARKER = "# --- Zabbix AI 漂浮球：注入前端脚本（install_launcher.py 管理，勿手改）---"
INJECT_LINES = (
    "sub_filter_once on;",
    "sub_filter_types text/html;",
    """sub_filter '</body>' '<script src="/ai/launcher.js" defer></script></body>';""",
)
PHP_LOCATION_RE = re.compile(r"^(\s*)location\s+~\s+\[\^/\]\\\.php")


def run(cmd, check=False):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if check and r.returncode != 0:
        print("!! 命令失败: %s\n%s" % (cmd, out))
        sys.exit(1)
    return r.returncode, out


def read(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def installed(text):
    return "ai/launcher.js" in text


def nginx_ok():
    code, out = run("nginx -t")
    return code == 0, out


def reload_nginx():
    run("systemctl reload nginx")


def final_check(retries=6, delay=1.5):
    """确认注入真的出现在页面上。

    注意：`systemctl reload nginx` 是优雅重载，旧 worker 会继续服务到连接结束，
    所以 reload 之后立刻请求可能仍由「旧配置的 worker」应答。实测踩过一次误报失败，
    因此这里重试几次再下结论。
    """
    for i in range(retries):
        code, body = run("curl -s -m 10 http://127.0.0.1:801/zabbix.php?action=dashboard.view")
        if "ai/launcher.js" in body:
            print("注入自检: OK，页面里已出现 launcher.js（第 %d 次尝试）" % (i + 1))
            return True
        time.sleep(delay)
    print("!! 注入自检失败：重试 %d 次仍未在页面里看到 launcher.js" % retries)
    print("   排查建议：nginx -T | grep -A3 sub_filter；curl -s http://127.0.0.1:801/zabbix.php?action=dashboard.view | grep launcher")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-check", action="store_true", help="跳过注入自检")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("!! 需要 root（nginx -t / reload nginx / 写 nginx 配置）")
        return 2
    if not os.path.isfile(args.config):
        print("!! 找不到 nginx 配置: %s（用 --config 指定）" % args.config)
        return 2

    text = read(args.config)

    if args.status:
        print("配置文件 : %s" % args.config)
        print("已安装   : %s" % ("是" if installed(text) else "否"))
        if installed(text):
            for ln in text.splitlines():
                if "launcher.js" in ln or "sub_filter" in ln:
                    print("   ", ln.strip())
        ok, out = nginx_ok()
        print("nginx -t : %s" % ("OK" if ok else "失败\n" + out))
        return 0 if ok else 1

    if args.uninstall:
        if not installed(text):
            print("未安装，无需卸载")
            return 0
        lines = text.splitlines(True)
        kept, removed = [], 0
        for ln in lines:
            s = ln.strip()
            if s == MARKER or s.startswith("sub_filter"):
                removed += 1
                continue
            kept.append(ln)
        backup = "%s.bak.uninstall.%s" % (args.config, time.strftime("%Y%m%d%H%M%S"))
        shutil.copy2(args.config, backup)
        write(args.config, "".join(kept))
        ok, out = nginx_ok()
        if not ok:
            shutil.copy2(backup, args.config)
            print("!! 卸载后 nginx -t 失败，已回滚:\n%s" % out)
            return 1
        reload_nginx()
        print("已卸载 %d 行并 reload nginx（备份: %s）" % (removed, backup))
        return 0

    # ---- 安装 ----
    if installed(text):
        print("已经装好了（幂等，不重复注入）")
        ok, out = nginx_ok()
        print("nginx -t : %s" % ("OK" if ok else "失败\n" + out))
        if not ok:
            return 1
        return 0 if (args.no_check or final_check()) else 1

    lines = text.splitlines(True)
    idx = None
    for i, ln in enumerate(lines):
        if PHP_LOCATION_RE.match(ln):
            idx = i
            break
    if idx is None:
        print("!! 没找到 PHP location 段（`location ~ [^/]\\.php`），请把下面几行手动加到该段内：")
        print("   " + "\n   ".join((MARKER,) + INJECT_LINES))
        return 1

    ws = re.match(r"\s*", lines[idx]).group(0)
    pad = ws + "    "
    block = ["%s%s\n" % (pad, MARKER)] + ["%s%s\n" % (pad, s) for s in INJECT_LINES]
    new_lines = lines[: idx + 1] + block + lines[idx + 1 :]

    backup = "%s.bak.launcher.%s" % (args.config, time.strftime("%Y%m%d%H%M%S"))
    shutil.copy2(args.config, backup)
    write(args.config, "".join(new_lines))
    print("已写入 %d 行到 %s 的 PHP location 段（备份: %s）" % (len(block), args.config, backup))

    ok, out = nginx_ok()
    if not ok:
        shutil.copy2(backup, args.config)
        print("!! nginx -t 失败，已回滚:\n%s" % out)
        return 1
    reload_nginx()
    print("nginx -t OK，已 reload")

    return 0 if (args.no_check or final_check()) else 1


if __name__ == "__main__":
    sys.exit(main())
