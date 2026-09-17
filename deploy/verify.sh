#!/usr/bin/env bash
# ============================================================================
# Zabbix AI 对话框插件 —— 部署【后】自检（只读）
#
# 用法：
#   bash deploy/verify.sh
#   MYSQL_PWD='mysql_root密码' ZBX_USER=Admin ZBX_PASS='zabbix管理员密码' \
#     bash deploy/verify.sh
#
# 可选环境变量：
#   APP_PORT   插件监听端口，默认 10801
#   BASE_URL   站点基地址，默认 http://127.0.0.1（Zabbix 前端所在地址）
#   SERVICE    systemd 服务名，默认 zabbix-ai-plugin
#   MYSQL_PWD  提供后检查 iframe sandbox 设置（关键项）
#   ZBX_USER / ZBX_PASS  提供后模拟真实浏览器（URL 编码 cookie）验证鉴权
#
# 退出码：0=全部通过；1=存在失败项
# ============================================================================
set -u

APP_PORT="${APP_PORT:-10801}"
BASE_URL="${BASE_URL:-http://127.0.0.1}"
AI="${BASE_URL}/ai"
SERVICE="${SERVICE:-zabbix-ai-plugin}"
API_PATH="${API_PATH:-/api_jsonrpc.php}"
PY="${PY:-python3}"

PASS=0
FAIL=0
ok()   { echo "  [PASS] $*"; PASS=$((PASS + 1)); }
bad()  { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }
skip() { echo "  [SKIP] $*"; }

echo "==================================================================="
echo " Zabbix AI 插件 部署后自检"
echo "==================================================================="

echo "--- 1. systemd 服务 ---"
[ "$(systemctl is-active "${SERVICE}" 2>/dev/null)" = "active" ] \
  && ok "服务运行中（active）" || bad "服务未运行（systemctl status ${SERVICE}）"
[ "$(systemctl is-enabled "${SERVICE}" 2>/dev/null)" = "enabled" ] \
  && ok "已设置开机自启" || bad "未设置开机自启（systemctl enable ${SERVICE}）"

echo "--- 2. 应用直连（绕过 nginx）---"
H="$(curl -s -m 10 "http://127.0.0.1:${APP_PORT}/api/health" 2>/dev/null || true)"
echo "  ${H:-（无响应）}"
echo "${H}" | grep -q '"ok":true'                   && ok "健康接口 ok"        || bad "健康接口无响应或异常"
echo "${H}" | grep -q '"mysql":true'                && ok "MySQL 连通"         || bad "MySQL 不通：检查 .env 的 MYSQL_PASSWORD 与 config.yaml 的 storage.mysql"
echo "${H}" | grep -q '"zabbix_api_configured":true' && ok "Zabbix API 已配置"  || bad "config.yaml 的 zabbix.api_url 未配置"

echo "--- 3. 经 nginx 反代访问 ---"
code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "${AI}/" 2>/dev/null || echo 000)"
[ "${code}" = "200" ] && ok "${AI}/ -> 200" \
  || bad "${AI}/ -> ${code}（检查 location ^~ /ai/、proxy_pass 端口、nginx 是否 reload）"

code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "${AI}/api/health" 2>/dev/null || echo 000)"
[ "${code}" = "200" ] && ok "${AI}/api/health -> 200" \
  || bad "${AI}/api/health -> ${code}（若为 404，几乎一定是 location 漏写 ^~）"

code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "${AI}/api/history" 2>/dev/null || echo 000)"
[ "${code}" = "401" ] && ok "无会话访问 /api/history -> 401（访问控制生效）" \
  || bad "无会话访问 -> ${code}，期望 401"

# 若上面的反代访问不通，很可能是 BASE_URL 少了 Zabbix 前端的端口（如 :801），
# 这里主动探测常见端口，给出可直接复制的正确命令，避免误判为部署失败。
if [ "${code}" != "401" ]; then
  origin="$(echo "${BASE_URL}" | sed -E 's#^(https?://[^:/]+).*#\1#')"
  found=""
  for p in ${PROBE_PORTS:-80 443 801 8080 8081 8000}; do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 "${origin}:${p}/ai/api/health" 2>/dev/null || echo 000)" = "200" ]; then
      found="${origin}:${p}"
      break
    fi
  done
  if [ -n "${found}" ]; then
    echo "  >>> 检测到可用地址：${found}/ai/"
    echo "  >>> 本机 Zabbix 前端不在默认端口，请改用："
    echo "      BASE_URL=${found} MYSQL_PWD='...' ZBX_USER=... ZBX_PASS=... bash deploy/verify.sh"
  else
    echo "  >>> 提示：请确认 BASE_URL 是否包含 Zabbix 前端端口（本参考环境为 :801）"
  fi
fi

echo "--- 4. Zabbix iframe sandbox 设置（必做项）---"
if [ -n "${MYSQL_PWD:-}" ]; then
  V="$(mysql -uroot -N -e "SELECT iframe_sandboxing_exceptions FROM zabbix.config" 2>/dev/null || true)"
  if echo "${V}" | grep -q "allow-scripts"; then
    ok "已放行脚本（iframe_sandboxing_exceptions='${V}'）"
  else
    bad "未放行脚本（当前='${V}'）→ dashboard 内页面 JS 不会执行：输入 / 无提示、发送键无反应"
    echo "         处理：Administration → General → Other → Iframe sandboxing exceptions"
    echo "               填 'allow-scripts allow-same-origin' 并保存"
  fi
else
  skip "未提供 MYSQL_PWD，无法检查（该项是 dashboard 内可用的前提，强烈建议检查）"
fi

echo "--- 5. 真实浏览器同款鉴权（URL 编码 cookie）---"
if [ -n "${ZBX_USER:-}" ] && [ -n "${ZBX_PASS:-}" ]; then
  TMP_PY="$(mktemp /tmp/zbxai-verify-XXXXXX.py)"
  cat > "${TMP_PY}" <<'PY'
import base64, json, os, urllib.error, urllib.parse, urllib.request

api = os.environ["ZBX_API"]
ai = os.environ["AI"]


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(api, data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


try:
    sid = rpc("user.login", {"username": os.environ["ZBX_USER"], "password": os.environ["ZBX_PASS"]})["result"]
except Exception as exc:  # 登录失败
    print("LOGIN_FAILED", exc)
    raise SystemExit(0)

raw = base64.b64encode(json.dumps({"sessionid": sid, "sign": "x"}).encode()).decode()
enc = urllib.parse.quote(raw, safe="")   # 模拟 PHP setcookie() 的 URL 编码

for label, value in (("encoded", enc), ("unencoded", raw)):
    req = urllib.request.Request(ai + "/api/history",
                                 headers={"Cookie": "zbx_session=" + value})
    try:
        code = urllib.request.urlopen(req, timeout=30).getcode()
    except urllib.error.HTTPError as exc:
        code = exc.code
    print(label, code)
PY
  OUT="$(ZBX_API="${BASE_URL}${API_PATH}" AI="${AI}" ZBX_USER="${ZBX_USER}" ZBX_PASS="${ZBX_PASS}" \
        "${PY}" "${TMP_PY}" 2>&1 || true)"
  rm -f "${TMP_PY}"
  echo "  ${OUT}" | sed 's/^/  /'
  echo "${OUT}" | grep -q "encoded 200"   && ok "URL 编码 cookie 鉴权通过（真实浏览器路径）" \
    || bad "URL 编码 cookie 鉴权失败（期望 200）"
  echo "${OUT}" | grep -q "unencoded 200" && ok "未编码 cookie 亦可用（兼容性）" \
    || bad "未编码 cookie 鉴权失败（期望 200）"
  echo "${OUT}" | grep -q "LOGIN_FAILED"  && bad "Zabbix 登录失败，请检查 ZBX_USER/ZBX_PASS"
else
  skip "未提供 ZBX_USER/ZBX_PASS，跳过（建议执行以覆盖真实浏览器路径）"
fi

echo "--- 6. 最近错误日志 ---"
ERR="$(journalctl -u "${SERVICE}" --no-pager -n 300 2>/dev/null | grep -E "ERROR|CRITICAL|Traceback" | tail -5 || true)"
if [ -n "${ERR}" ]; then echo "${ERR}" | sed 's/^/  /'; else echo "  无 ERROR 级日志"; fi
CNT="$(journalctl -u "${SERVICE}" --no-pager -n 300 2>/dev/null | grep -c "鉴权失败" || true)"
echo "--- 7. 近期鉴权失败次数（仅参考）---"
echo "  近 300 行日志中 ${CNT:-0} 次；未登录访问或会话过期都会产生，属正常现象"

echo "==================================================================="
echo " 自检结果：PASS=${PASS}  FAIL=${FAIL}"
if [ "${FAIL}" -gt 0 ]; then
  echo " 存在失败项，请按上面提示处理。"
  exit 1
fi
echo " 全部通过。请再到浏览器验证：对话框为亮色、输入 / 弹出供应商、发送有回复。"
