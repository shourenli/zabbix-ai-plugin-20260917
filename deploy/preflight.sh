#!/usr/bin/env bash
# ============================================================================
# Zabbix AI 对话框插件 —— 部署【前】环境检查（只读，不修改系统）
#
# 用法：
#   bash deploy/preflight.sh
#   MYSQL_PWD='你的mysql_root密码' bash deploy/preflight.sh
#
# 可选环境变量：
#   APP_PORT   插件监听端口，默认 10801
#   MYSQL_PWD  MySQL root 密码（提供后才会检查数据库）
# ============================================================================
set -u

APP_PORT="${APP_PORT:-10801}"
FAIL=0
WARN=0

ok()   { echo "  [OK]   $*"; }
warn() { echo "  [WARN] $*"; WARN=$((WARN + 1)); }
bad()  { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }

echo "==================================================================="
echo " Zabbix AI 插件 部署前检查"
echo "==================================================================="

echo "--- 1. 操作系统 ---"
if [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "  ${PRETTY_NAME:-unknown}"
else
  warn "无法读取 /etc/os-release"
fi
echo "  kernel: $(uname -r)"

echo "--- 2. Python 与 venv 支持 ---"
if command -v python3 >/dev/null 2>&1; then
  echo "  $(python3 -V 2>&1)"
  if python3 -c "import ensurepip" >/dev/null 2>&1; then
    ok "ensurepip 可用（python3-venv 已安装）"
  else
    bad "缺少 ensurepip —— 直接建 venv 会报 'ensurepip is not available'"
    echo "         请先执行：sudo apt update && sudo apt install -y python3-venv python3-pip"
    echo "         （Ubuntu 24.04 若报找不到包，用 python3.12-venv）"
  fi
else
  bad "未安装 python3"
fi

echo "--- 3. MySQL ---"
if command -v mysql >/dev/null 2>&1; then
  echo "  $(mysql --version)"
  echo "  mysql 服务: $(systemctl is-active mysql 2>/dev/null)"
  if [ -n "${MYSQL_PWD:-}" ]; then
    if mysql -uroot -e "SELECT 1" >/dev/null 2>&1; then
      ok "root 连接正常"
      if mysql -uroot -e "SHOW DATABASES" 2>/dev/null | grep -qw zabbix_ai_plugin; then
        ok "已存在 zabbix_ai_plugin 库"
        if mysql -uroot -e "SHOW TABLES" zabbix_ai_plugin 2>/dev/null | grep -qw sessions; then
          ok "sessions / audit_log 表已存在"
        else
          warn "表不存在（可用 deploy/schema.sql 预建，或由应用启动时自动建）"
        fi
      else
        warn "zabbix_ai_plugin 库不存在 —— 稍后需执行 deploy/schema.sql"
      fi
    else
      warn "root 连接失败，请确认 MYSQL_PWD 是否正确"
    fi
  else
    warn "未提供 MYSQL_PWD，跳过数据库连通性检查"
  fi
else
  bad "未安装 mysql 客户端"
fi

echo "--- 4. Zabbix ---"
if command -v zabbix_server >/dev/null 2>&1; then
  echo "  $(zabbix_server -V 2>/dev/null | head -1)"
else
  warn "未找到 zabbix_server 命令（若 Zabbix 在其它主机，可忽略）"
fi
echo "  zabbix-server: $(systemctl is-active zabbix-server 2>/dev/null)"
echo "  zabbix-agent : $(systemctl is-active zabbix-agent 2>/dev/null)"

echo "--- 5. nginx ---"
if command -v nginx >/dev/null 2>&1; then
  nginx -v 2>&1 | sed 's/^/  /'
  if nginx -t >/dev/null 2>&1; then
    ok "nginx 配置语法正常"
  else
    bad "nginx 配置语法错误，请先修复：nginx -t"
  fi
else
  bad "未安装 nginx"
fi

echo "--- 6. 监听端口（请确认哪个是 Zabbix 前端）---"
ss -tln 2>/dev/null | awk 'NR>1 {print $4}' | sed 's/^/  /' | sort -u
echo "  提示：/ai/ 必须加在【提供 Zabbix 前端】的那个 server 块里"
echo "        （常见为 80/443，本参考环境为 801；80 可能只是 nginx 默认站点）"

echo "--- 7. 插件端口占用 ---"
SERVICE="${SERVICE:-zabbix-ai-plugin}"
if ss -tln 2>/dev/null | grep -q ":${APP_PORT} "; then
  if systemctl is-active "${SERVICE}" >/dev/null 2>&1; then
    ok "端口 ${APP_PORT} 由本插件服务占用（属重复部署/升级场景）"
  else
    bad "端口 ${APP_PORT} 已被其它进程占用：$(ss -tlnp 2>/dev/null | grep ":${APP_PORT} " | head -1)"
    echo "         可改 config.yaml 的 server.port，并同步 systemd 与 nginx 三处"
  fi
else
  ok "端口 ${APP_PORT} 空闲"
fi

echo "--- 8. 资源 ---"
free -m 2>/dev/null | head -2 | sed 's/^/  /'
df -h / 2>/dev/null | tail -1 | sed 's/^/  /'
if command -v getenforce >/dev/null 2>&1; then
  echo "  SELinux: $(getenforce 2>/dev/null)"
fi

echo "==================================================================="
echo " 检查结果：FAIL=${FAIL}  WARN=${WARN}"
if [ "${FAIL}" -gt 0 ]; then
  echo " 存在必须修复的问题，请处理后再部署。"
  exit 1
fi
echo " 未发现阻断性问题，可以继续部署。"
