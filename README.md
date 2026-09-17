# Zabbix AI 对话框插件（zabbix-ai-plugin）

**简体中文** ｜ [English](README.en.md)

**许可证：[GNU General Public License v3.0](LICENSE)**（全文见文末「许可证」一节）

> **当前对应版本：v1.1.0**（2026-09-17）｜版本历史见 [`CHANGELOG.md`](CHANGELOG.md)｜
> 本仓库自 `v1.1.0` 起为**重建的干净历史**：不含任何真实凭据、真实主机名或真实 IP
> （清理范围与处置说明见 [`SECURITY.md`](SECURITY.md)）。

在 Zabbix 的 dashboard 里嵌入一个 AI 对话框：用自然语言查告警、查指标、做趋势预测，
并用 `/供应商名` 把问题一键发到对应供应商的钉钉群。

- **部署目标**：Ubuntu 22.04 / 24.04 + Zabbix 6.0 LTS 或 7.0 + MySQL 8.0 + nginx
- **访问方式**：登录 Zabbix 后从 dashboard 进入；未登录直接访问 `/ai/` 无法使用（见 4.4）
- **不改 Zabbix 自身文件**：唯一可选改动是 nginx 注入的漂浮聊天球（见 4.3，可 `--uninstall` 撤销）

## 文档地图

| 文档 | 读者 | 内容 |
|---|---|---|
| **README.md**（本文） | 使用者 / 运维 | 它怎么工作、环境要求、快速开始、使用说明、**配置参考**、排错、开发测试、目录结构、已知限制 |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | 生产部署者 | 交付物清单、离线包打包与部署、部署前检查、逐步部署、Zabbix 侧必做设置、加固清单、升级回滚、数据保留、附录踩坑 |
| [`SECURITY.md`](SECURITY.md) | 所有人 | 漏洞报告渠道、信任边界、有意为之的安全设计、已知限制、历史密钥遗留说明 |
| [`RECOVERY-IDC.md`](RECOVERY-IDC.md) | IDC 现场人员 | 现场恢复清单（可打印带走） |
| [`CHANGELOG.md`](CHANGELOG.md) | 所有人 | v1.1.0 与 v1.0.0 – v1.0.9 每个版本「现象 → 原因 → 现在怎样」 |
| [`AGENTS.md`](AGENTS.md) | 开发者 | 改动约定（每次改动一个 commit、交付前测试全绿） |
| [`LICENSE`](LICENSE) | 所有人 | GNU General Public License v3.0 全文 |
| [`README.en.md`](README.en.md) | English readers | 本文的英文版（English translation of this document） |

**生产部署请看 `DEPLOYMENT.md`**：本文第 3 节只保证「能在一台干净机器上跑起来」，
生产所需的加固、回滚、数据保留策略都在那边，两者不一致时以 `DEPLOYMENT.md` 为准。

---

## 目录

1. [它是怎么工作的](#1-它是怎么工作的)
2. [环境要求](#2-环境要求)
3. [快速开始](#3-快速开始)
4. [使用说明](#4-使用说明)
5. [配置参考](#5-配置参考)
6. [升级与日常运维](#6-升级与日常运维)
7. [排错与常见问题](#7-排错与常见问题)
8. [开发与测试](#8-开发与测试)
9. [目录结构](#9-目录结构)
10. [已知限制](#10-已知限制)

---

## 1. 它是怎么工作的

```
Zabbix 前端 (dashboard)
  └─ URL widget (iframe)  ── 同源路径 /ai/ ──▶  nginx
                                                   │ 反代
                                                   ▼
                                     FastAPI 应用 (127.0.0.1:10801)
                                       ├─ 校验 zbx_session cookie ──▶ 未登录返回 401
                                       ├─ 调 LLM（OpenAI 兼容接口）→ 由模型决定调用哪个工具
                                       ├─ 五个工具（注册给模型）：
                                       │    query_problems / query_top / query_metrics
                                       │    query_hosts  / trend_analysis
                                       ├─ /供应商名 命令：由**代码**确定性执行钉钉发送
                                       │    （模型**没有**发送工具，见 4.2）
                                       └─ MySQL：只存插件自己的两张表
                                            ├─ sessions   对话历史（按**登录会话**隔离）
                                            └─ audit_log  审计（谁、何时、发给哪个供应商）
```

两个关键点：

- **监控数据全部通过 Zabbix JSON-RPC API 获取**，并携带登录用户的会话（`sessionid` 透传）。
  所以每个用户只能看到自己权限范围内的数据，插件**不读 Zabbix 的数据库表**。
- **MySQL 只存插件自己的数据**，因此用独立库 + 独立最小权限账号，不复用 zabbix 的库和账号
  （原因见 3.1）。

---

## 2. 环境要求

| 项目 | 要求 | 检查方式 |
|---|---|---|
| 操作系统 | Ubuntu 22.04 / 24.04 | `lsb_release -a` |
| Zabbix | 6.0 LTS 或 7.0（含前端与 API） | `zabbix_server -V` |
| MySQL | 8.0 | `mysql --version` |
| nginx | 任意近期版本 | `nginx -v` |
| Python | 3.10 及以上，且需 `python3-venv` | `python3 -V`（见下方说明） |
| LLM | 一个支持 **Function Calling** 的 OpenAI 兼容 API Key | 见 5.4 |

> **Ubuntu 需先装 venv 支持**：Ubuntu 22.04/24.04 默认不带 `ensurepip`，
> 直接 `python3 -m venv` 会报 "ensurepip is not available"。先执行：
>
> ```bash
> sudo apt update && sudo apt install -y python3-venv python3-pip
> ```
>
> 若提示找不到 `python3-venv`，按实际版本装，例如 Ubuntu 24.04：`python3.12-venv`。

---

## 3. 快速开始

下面命令里的域名、密码、token 请替换成实际值。**生产请改用 `DEPLOYMENT.md` 的离线包流程
（含部署前检查 `deploy/preflight.sh` 与部署后自检 `deploy/verify.sh`）。**

### 3.1 建数据库

`deploy/schema.sql` 已按「建库 → 建账号授权 → 建表」写好，改掉里面的 `'strong-password'` 后执行：

```bash
sudo mysql < deploy/schema.sql
sudo mysql -e "SHOW TABLES;" zabbix_ai_plugin          # 应看到 sessions、audit_log
```

两种权限模式按 DBA 要求选（详见 5.3）：

| 模式 | `config.yaml` | 账号权限 | 谁建表 |
|---|---|---|---|
| **A（推荐，默认）** | `auto_init: true` | `SELECT INSERT UPDATE DELETE CREATE INDEX` | 应用启动时自动建表 |
| **B（DBA 管控）** | `auto_init: false` | `SELECT INSERT UPDATE DELETE` | 由 `deploy/schema.sql` 预先建好 |

> 数据库不在本机时，把 `schema.sql` 里账号的 host（`'zabbix_ai'@'127.0.0.1'`）改成应用服务器 IP，
> 并同步改 `config.yaml` 的 `storage.mysql.host`。

### 3.2 放置代码并装依赖

```bash
sudo mkdir -p /opt/zabbix-ai-plugin
# 把本仓库内容放进 /opt/zabbix-ai-plugin（离线包则解压到该目录）
sudo chown -R zabbix:zabbix /opt/zabbix-ai-plugin
cd /opt/zabbix-ai-plugin
sudo -u zabbix python3 -m venv .venv
sudo -u zabbix .venv/bin/pip install -r requirements.txt
```

国内网络可用镜像加速：`.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`

### 3.3 写 `.env`（密钥都在这里）

```bash
cd /opt/zabbix-ai-plugin
sudo -u zabbix cp .env.example .env
sudo -u zabbix vi .env          # 变量清单见 5.2
sudo chmod 600 /opt/zabbix-ai-plugin/.env
```

### 3.4 改 `config.yaml`

最少改 `zabbix.api_url` 与 `suppliers`，字段全表见 5.1：

```yaml
zabbix:
  api_url: "http://你的zabbix地址:801/api_jsonrpc.php"   # ← 必改

suppliers:                                               # ← 按实际供应商改
  SUPPLIER_A:
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_SUPPLIER_A}"
    keyword: "告警"
```

### 3.5 前台试跑并自检

```bash
sudo -u zabbix .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 10801
curl -s http://127.0.0.1:10801/api/health
# 期望：{"ok":true,"mysql":true,"llm_model":"glm-4-flash","zabbix_api_configured":true}
```

- `mysql` 为 `false` → 查 `.env` 的 `MYSQL_PASSWORD` 与 `storage.mysql` 配置
- 浏览器直接打开 `http://127.0.0.1:10801/` 会提示"会话已失效"——**这是正常的**（没有 Zabbix 会话）

> ⚠️ **端口由启动命令决定**：`--port 10801` 来自 systemd 的 `ExecStart`（见
> `deploy/zabbix-ai-plugin.service`），`config.yaml` 里的 `server.port` **不参与监听**（见 10）。

### 3.6 上 systemd

```bash
sudo cp /opt/zabbix-ai-plugin/deploy/zabbix-ai-plugin.service /etc/systemd/system/
sudo vi /etc/systemd/system/zabbix-ai-plugin.service   # 按需改 User/WorkingDirectory/EnvironmentFile/ExecStart
sudo systemctl daemon-reload
sudo systemctl enable --now zabbix-ai-plugin
curl -s http://127.0.0.1:10801/api/health
```

### 3.7 配 nginx 反代（两个必踩的坑）

**`deploy/nginx.conf.example` 是片段示例，不能直接启用**（`server_name`/证书/`location /` 都是占位）。
正确做法：把下面这段加进**你现在提供 Zabbix 前端的那个 `server { }` 块里**（端口要与 Zabbix 前端一致）：

```nginx
    location ^~ /ai/ {
        proxy_pass http://127.0.0.1:10801/;   # 结尾的 / 不能少：它会去掉 /ai 前缀
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
```

> ⚠️ **必须写 `location ^~ /ai/`，不能写 `location /ai/`**：Zabbix 自带配置里有一条
> `location ~ /(api\/|conf[^\.]|include|locale) { deny all; return 404; }`，
> 而本插件接口形如 `/ai/api/health`（路径含 `api/`）会被它命中返回 404。
> 加 `^~` 后前缀匹配**跳过正则检查**。

> ⚠️ **必须与 Zabbix 前端同源（同域名 + 同端口）**：插件靠浏览器自动携带的 `zbx_session`
> cookie 判断你是谁。加在别的 server 块（例如 80 端口的默认站点）就是跨域，cookie 不发送，
> 页面会一直提示"会话已失效"。判断方法：`ss -tlnp | grep nginx` 看哪个端口是 Zabbix 前端。

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -s http://你的zabbix地址:801/ai/api/health     # 应返回同一份 health JSON
```

### 3.8 dashboard 里加对话框

1. 登录 Zabbix → **Dashboards** → 打开目标 dashboard → **Edit dashboard** → **Add** → **URL** widget
2. **Name**：`AI 助手`；**URL**：`http://你的zabbix地址:801/ai/`（端口与 Zabbix 前端一致）
3. **Add** → **Save**；建议至少占半屏宽、整列高

### 3.9 ⚠️ 必做：关掉 Zabbix 的 iframe 沙箱限制

Zabbix 6.0/7.0 在 **Administration → General → Other** 有 **Use iframe sandboxing**（**默认开启**）。
开启且 `Iframe sandboxing exceptions` 为空时，URL widget 的 iframe 会带上 `sandbox=""`，
**脚本、同源、Cookie 全部被禁用**。症状是：对话框能显示，但**输入 `/` 不弹出供应商名、点发送毫无反应**，
nginx 日志里完全没有 `/ai/api/*` 请求。

**处理**：把 **Iframe sandboxing exceptions** 填成 `allow-scripts allow-same-origin`（推荐），
或取消勾选 **Use iframe sandboxing**（对所有 URL widget 生效，安全性更低）。
命令行等价操作（把 `<API_TOKEN>` 换成你的 API Token）：

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"settings.update","params":{"iframe_sandboxing_exceptions":"allow-scripts allow-same-origin"},"auth":"<API_TOKEN>","id":1}' \
  http://你的zabbix地址:801/api_jsonrpc.php
```

> 该设置对**所有** URL widget 全局生效；若还有其它 URL widget 承载不可信外部内容，请评估风险，
> 或改用替代方案：把 `/ai/` 作为独立页面打开（不受 iframe sandbox 影响，功能一致）。
> 详见 `DEPLOYMENT.md` 6.1。

### 3.10 部署验收清单

- [ ] `curl http://127.0.0.1:10801/api/health` → `mysql: true`
- [ ] `curl http://你的zabbix地址:801/ai/api/health` → 同上
- [ ] dashboard 里能看到 AI 对话框
- [ ] 输入框打一个 `/` → 下方弹出供应商名（没有则几乎一定是 3.9 的 iframe sandbox 问题）
- [ ] 输入一句话点"发送" → 有回复（若无反应，同上）
- [ ] 退出 Zabbix 登录后访问 `/ai/` → 无法使用（提示会话已失效，见 4.4）
- [ ] 问"最近有哪些告警" → 返回真实告警
- [ ] `/SUPPLIER_A 测试消息` → 钉钉群收到消息

---

## 4. 使用说明

### 4.1 自然语言问答

直接打字提问即可，插件会调相应工具取真实数据。注册给模型的工具共 **5 个**：

| 工具 | 用途 |
|---|---|
| `query_problems` | 当前未恢复的问题 / 某时段的历史故障（含已恢复、恢复时间与时长），可按主机过滤 |
| `query_top` | 服务端聚合排名：某时段哪些主机/触发器告警最多（TOP N） |
| `query_metrics` | 取监控项数值：最新值，或某时段的 min/max/avg/first/last；不传 `key` 时返回该主机真实存在的 key 族清单 |
| `query_hosts` | 查主机清单与接口信息（IP/端口/DNS/接口类型） |
| `trend_analysis` | 读趋势数据线性外推（如"磁盘还有几天满"） |

典型问法：

| 你可以问 | 插件做的事 |
|---|---|
| `最近有哪些告警？` / `现在有什么在报警？` | `query_problems`（scope=current）查**当前未恢复**的问题 |
| `今天上午有什么故障吗？` / `昨天出了哪些问题？` | `query_problems`（scope=history）查该时段**全部故障，含已恢复** |
| `db01 的 CPU 使用率现在多少？` | `query_metrics` 查该主机监控项最新值 |
| `db01 磁盘还有几天满？容量上限 500` | `trend_analysis` 线性外推 |
| `最近7天哪几台机器告警最多？` / `本月告警最多的设备 TOP10` | `query_top`：服务端聚合后按主机/触发器排名 |
| `DEMO_CARRIER01 的 IP 是多少？` / `db01 的端口是什么？` | `query_hosts`（带 `search`）返回 `ip`/`port`/接口类型；`useip=0` 的主机返回 DNS 名 |
| `DEMO_SDWAN01 本月运行状况分析` | `query_problems`（带 `host`）只统计该主机，再 `query_metrics` 汇总 min/max/avg |
| `DEMO_SDWAN02 告警时间段的带宽使用情况` | `query_metrics`（`host` + `key` + `period`）给出该时段统计 |
| `db01 有哪些监控项？`（不知道 key 时） | `query_metrics` **不传 `key`**：返回真实 key 族清单（含单位与样本 key），可照抄重试 |

**监控项 key 不用自己记**：agent 主机与 SNMP 设备 key 命名完全不同
（前者如 `system.cpu.load`，后者如 `net.if.in[ifHCInOctets.15]`）。
不确定 key 时不写 `key` 直接问，插件会先列出该主机**真实存在**的监控项；
找不到 key 时它**不会**编造，也**不会**把"key 没猜中"说成"没有数据"。

**支持的时间段**（这 14 个是工具参数的合法取值，模型负责把你说的话映射过来）：

| 键 | 含义 | 键 | 含义 |
|---|---|---|---|
| `today` | 今天 00:00 至今 | `last_night` | 昨晚 18:00-24:00 |
| `early_morning` | 今天凌晨 00:00-06:00 | `this_month` | 本月 1 日 00:00 至今 |
| `morning` | 今天上午 00:00-12:00 | `last_month` | 上月 1 日 00:00 至本月 1 日 00:00 |
| `noon` | 今天中午 11:00-14:00 | `last_1h` | 最近 1 小时 |
| `afternoon` | 今天下午 12:00-18:00 | `last_24h` | 最近 24 小时 |
| `evening` | 今天晚上 18:00-24:00 | `last_7d` | 最近 7 天 |
| `yesterday` | 昨天全天 | `last_30d` | 最近 30 天 |

服务端**自己也能识别**这些中文口语词（按顺序匹配，长词优先）：

```
昨天晚上 昨晚 昨天下午 昨天上午 今天中午 今天下午 今天上午 今天凌晨 今天晚上
凌晨 半夜 深夜 清晨 早晨 早上 上午 中午 正午 下午 午后 傍晚 晚上 晚间 夜里
前天 昨天 昨日 今天 今日 本日
```

> **回答里会明确写出实际查询的时间范围**，所以你能一眼看出它查的是「今天中午 11:00-14:00」
> 还是「最近 24 小时」。如果某个时段没被识别，它会**明说**（`⚠ 未能识别时段 'X'`），
> 而**不会悄悄换成另一个时间段**。
> 注意：中文**时长**词（"最近7天""最近1小时"）不在上面的口语词表里，这类说法靠模型映射成
> `last_7d` 等键；"昨天夜里"目前有坑，见 10。
> 历史故障来自 Zabbix 事件表，保留期由 housekeeping 决定（常见 365 天）；
> 这些是**按事件发生时间**统计的，不代表此刻仍在报警。

### 4.2 `/供应商名` 命令（把问题发给供应商）

**名字在哪里设定？** `config.yaml` 的 `suppliers:` 下，**每个键名就是一个供应商**，也就是斜杠后面要写的名字：

```yaml
suppliers:
  网络供应商A:            # ← 这个名字就是命令里的 /网络供应商A
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_NETWORK_A}"
    keyword: "告警"
  机房供应商B:            # ← 第二个供应商，命令是 /机房供应商B
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_IDC_B}"
```

**能配几个？** 数量不限。但输入 `/` 时下方**最多列出 10 个**候选（见 10），更多供应商可用前缀过滤。

**语法**：`/供应商名 消息内容`，例如 `/网络供应商A 出口丢包严重，请协助排查，联系人张三 138xxxx`

**会自动提示吗？** 会：输入框里打一个 `/` 就列出已配置供应商并**默认高亮第一项**，全程不用碰鼠标。

| 按键 | 作用 |
|---|---|
| `↑` / `↓` | 在候选之间移动（到头循环） |
| `Enter` 或 `Tab` | 选中当前高亮项，自动填入 `/供应商名 ` 并收起列表 |
| `Esc` | 收起列表 |

补全后接着写消息，再按 `Enter` 就是发送——**一旦你打了空格（开始写正文），`Enter` 就恢复为发送**，
不会再被补全抢走。鼠标点击候选同样可用。

> 只命中**一个**候选时（例如已经输入 `/SUPPLIER_A`），方向键**不会被占用**——
> 此时"选择"没有意义，`Enter`/`Tab` 直接补全，方向键留给光标正常使用。

底部提示栏常驻显示当前可用供应商名单。

**前缀匹配规则**

- 名字可以简写，前提是**唯一命中**：配了"网络供应商A"时，`/网络` 也能用；
- 前缀命中**多个**时**拒绝发送**并列出可选名称，避免发错群；
- 名字**不在白名单**里一律拒绝，并返回可选列表。

**发送后的表现**

- 消息格式：`[zabbix-AI] 发送人 上报：` + 消息内容（可配置，见 5.1 的 `dingtalk.message_template`）
- 成功：对话框回复发送成功、目标供应商、发送时间；失败：钉钉的错误信息（如 `errcode=310000`）原样呈现并写审计
- **发送由代码确定性执行，不依赖 AI 判断**：显示"已发送"就表示钉钉接口确实返回成功
- **不能用自然语言触发**：AI **没有**发送工具。像"帮我给某供应商发条消息"这类请求，
  插件会引导改用 `/供应商名 消息内容`，**不会**替你发出去。这是刻意的：发送有真实外部副作用，
  交给大模型自主决定，既可能"没发却说发了"，也可能把与监控无关的内容发进供应商群。
- 只输入 `/` 或格式不完整时，回复用法说明与可用供应商列表，不发送任何消息

**钉钉机器人怎么配？**

1. 钉钉群 → 群设置 → 智能群助手 → 添加机器人 → **自定义** → 复制 Webhook 地址
2. 安全设置三选一，按下表填 `config.yaml`：

| 机器人后台设置 | `config.yaml` 要填 |
|---|---|
| **自定义关键词**（推荐，最简单） | `keyword: "告警"`，与后台关键词**完全一致** |
| **加签** | `secret_env: "DINGTALK_NETWORK_A_SECRET"`，密钥填进 `.env` 对应变量 |
| **IP 白名单** | 不用填，把服务器出口公网 IP 加白即可 |
| **无安全设置**（仅靠 token） | 两个都不填，只给 `webhook` 即可（已实测可用） |

```yaml
suppliers:
  网络供应商A:
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_NETWORK_A}"
    keyword: "告警"                              # 关键词模式
    # secret_env: "DINGTALK_NETWORK_A_SECRET"    # 加签模式（可与 keyword 并存）
    # message_template: "【来自监控】{message}"   # 可选：该供应商单独的消息模板

  # 无安全设置时的写法（不填 keyword / secret_env）
  SUPPLIER_A:
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_SUPPLIER_A}"
```

**消息正文格式**（默认 `[zabbix-AI] {username} 上报：\n{message}`）可自定义，改配置即可、不用改代码：

```yaml
dingtalk:
  message_template: "[zabbix-AI] {username} 上报：\n{message}"
# 只想发正文（去掉"某某 上报"）：  message_template: "{message}"
```

占位符：`{username}` 发送人、`{message}` 消息正文。供应商级 `message_template` 优先于全局；
改完重启服务生效。兼容写法：`供应商名: "https://...webhook..."` 直接给 URL 字符串（无关键词、无加签）。

**钉钉常见报错**

| errcode | 原因 | 处理 |
|---|---|---|
| `310000` | 关键词不匹配（`keywords not in content`） | 让 `keyword` 与机器人后台一致 |
| `310000` | 加签校验失败（`sign not match`） | 配好 `secret_env` 并在 `.env` 填密钥 |
| `310000` | 发送方 IP 不在白名单 | 加白服务器出口 IP，或改用关键词/加签 |
| `300001` | `access_token` 无效 | 检查 `.env` 里的 token |
| `40035` | token 为空 | `.env` 里的 `DINGTALK_*` 变量没填或名字与 `config.yaml` 的 `${...}` 不一致 |

### 4.3 漂浮聊天球（可选）

不想让对话框占 dashboard 网格时，可做成**右下角悬浮球**：点击弹出小面板内嵌对话，
**不跳转、不刷新**；小球可拖动，位置记在 `localStorage`。

```bash
# 在 Zabbix 前端所在机器上执行（需要 root）
sudo python3 deploy/install_launcher.py                  # 安装（幂等，可重复执行）
sudo python3 deploy/install_launcher.py --status         # 查看状态
sudo python3 deploy/install_launcher.py --uninstall      # 卸载
sudo python3 deploy/install_launcher.py --config /etc/nginx/conf.d/zabbix.conf   # 指定 nginx 配置（默认即此路径）
sudo python3 deploy/install_launcher.py --no-check       # 跳过安装后的自检
```

它做了什么：在 nginx 的 PHP location 里加 3 行 `sub_filter`，把
`<script src="/ai/launcher.js" defer></script>` 注入到每个 HTML 页面的 `</body>` 前。
**Zabbix 自身的文件一个都不改**，所以 `apt upgrade zabbix-frontend-php` 不会覆盖本改动；
撤销就 `--uninstall`（脚本改动前先备份，`nginx -t` 不通过会自动回滚）。

显示范围与权限：

- **只在 dashboard 页面且已登录时出现**：`launcher.js` 校验 URL 里的 `action=dashboard.view`
  与页面上的个人资料入口，登录页/告警列表等页面**一颗球都不会创建**
- iframe 同源加载 `/ai/`，浏览器自动带上 `zbx_session`，所以**每个人看到的仍是自己权限内的数据**
- 小球与 URL widget 可以并存（两个入口指向同一个对话页）

> 排查：`curl -s http://<前端>:801/zabbix.php?action=dashboard.view | grep launcher.js`
> 应能看到注入的 script 标签；`/ai/launcher.js` 直接访问应返回 JS。
> 注意：安装脚本的**自检硬编码了 `127.0.0.1:801`**，Zabbix 前端不在 801 时自检会失败（用 `--no-check` 跳过）。

### 4.4 会话、接口与审计

**HTTP 接口**（由 nginx 转发到本应用；浏览器一侧的路径前缀始终是 `/ai/`——注意 `server.base_path` 并不生效，见 10）：

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| `GET` | `/` | 无（返回 200 HTML） | 聊天页面本身。**页面可打开 ≠ 已登录**：前端再调 `/api/*`，拿到 401 后清空界面并提示"会话已失效" |
| `GET` | `/api/health` | 无 | 自检：`{"ok","mysql","llm_model","zabbix_api_configured"}` |
| `GET` | `/license` | 无 | **许可证全文**（GNU GPL v3.0）；界面右上角「关于」入口也链接到这里 |
| `POST` | `/api/chat` | 需要 | 提交流水：`{"message": "...", "clear": false}` → `{"reply": "...", "context_reset": bool}` |
| `GET` | `/api/history` | 需要 | 当前会话历史、可用供应商、`ttl_minutes` |
| `GET` | `/api/audit` | 需要（**roleid=3 超级管理员**） | 审计记录：谁、何时、发给哪个供应商 |

未携带有效 `zbx_session` 时上述需要鉴权的接口返回 **401**；Zabbix API 不可达时返回 **503**
（刻意区分：503 不会被误读成"你没登录"）。

**会话历史按「登录会话」隔离**，不是按用户：

- **注销后重新登录 = 全新对话**：Zabbix 每次登录都下发新的 `sessionid`，插件用它当对话的键；
  同一账号开两个浏览器也是各自独立的对话。
- **闲置超时自动清空**：最后一条消息距今超过 `history.ttl_minutes`（默认 **15 分钟**）时，
  下一次打开或提问会自动开始新会话，并删除旧记录。界面会提示
  「上一轮对话已闲置超过 N 分钟，已开始新会话」，避免"你看到的"和"模型记得的"不一致。
- 点右上角"清空会话"可随时手动清除当前会话。
- 兜底清理：超过 `history.retention_days`（默认 7 天）的记录在服务启动时删除。
- **审计日志**：每次钉钉发送（含失败）都会记录发送人、供应商、时间、内容摘要。

### 4.5 主题（配色跟随 Zabbix）

对话框默认**跟随 Zabbix 主题**：读取父页面的主题类（`theme-blue` / `theme-dark` 等）自动切换亮/暗，
不会出现"Zabbix 白底、插件黑底"的违和感。

- widget 内「**主题**」按钮可在 `跟随 / 亮 / 暗` 之间切换（选择记在浏览器本地）
- 也可用 URL 强制指定：`http://你的zabbix地址:801/ai/?theme=light`
- 独立打开页面、读不到父页面主题时，回退到系统偏好（`prefers-color-scheme`）

---

## 5. 配置参考

### 5.1 `config.yaml` 字段全表

密钥（API Key / 数据库密码 / access_token / 加签密钥）一律通过环境变量注入，**不写进本文件**。

| 字段 | 说明 |
|---|---|
| `zabbix.frontend_url` | 前端入口地址，**仅展示用**（构建同源路径提示），代码不读它做判断 |
| `zabbix.api_url` | **必改**。Zabbix JSON-RPC 地址，通常 `http://域名:801/api_jsonrpc.php` |
| `zabbix.auth.mode` | 保持 `user`：透传登录用户会话，权限按用户隔离。**`service` 目前未贯通**，见 10 |
| `llm.base_url` | LLM 接口地址（OpenAI 兼容），见 5.4 |
| `llm.model` | 模型名，见 5.4 |
| `llm.api_key_env` | 存放 API Key 的**环境变量名**（不是 Key 本身） |
| `llm.temperature` | 采样温度，默认 `0.3`（工具调用需要确定性，不建议调高） |
| `llm.top_p` | 采样 top_p，仓库 `config.yaml` 为 `0.9`；**删掉该行则回落到代码内建默认 `1.0`** |
| `llm.max_tokens` | 单次回复上限，默认 `1024` |
| `llm.function_calling` | **必须保持 `true`**。置 `false` 时请求里**完全不发 tools**，5 个工具全部不可用（模型只能凭自身知识回答）。代码里没有"提示词式解析"降级路径 |
| `llm.extra_params` | 透传给后端的额外参数（按后端支持情况填，默认空） |
| `dingtalk.message_template` | 钉钉消息正文模板，默认 `[zabbix-AI] {username} 上报：\n{message}`；占位符 `{username}`/`{message}` |
| `suppliers` | 供应商白名单，键名 = `/命令`的名字，见 4.2 |
| `server.host` / `server.port` / `server.base_path` | **当前不生效**（仅被解析、无人使用）。监听地址与端口由 systemd 的 `ExecStart` 决定；路径前缀由 nginx 决定。见 10 |
| `history.ttl_minutes` | 闲置多久算新会话，默认 `15`；`0` = 不自动清空 |
| `history.max_messages` | 送入模型的历史条数上限，默认 `50` |
| `history.retention_days` | 兜底清理：启动时删除超过这么多天的记录，默认 `7`；`0` = 不清理 |
| `storage.mysql.host` / `storage.mysql.port` / `storage.mysql.database` / `storage.mysql.user` / `storage.mysql.charset` | 数据库连接参数（默认 `127.0.0.1` / `3306` / `zabbix_ai_plugin` / `zabbix_ai` / `utf8mb4`） |
| `storage.mysql.password_env` | 存放数据库密码的环境变量名，默认 `MYSQL_PASSWORD` |
| `storage.mysql.auto_init` | `true` 应用启动时自动建表（需 CREATE 权限）；`false` 由 DBA 预建表。见 5.3 |

### 5.2 `.env` 变量全表

| 变量 | 用途 |
|---|---|
| `LLM_API_KEY` | LLM 的 API Key（**必填**） |
| `MYSQL_PASSWORD` | 数据库密码（**必填**，与 `schema.sql` 一致） |
| `DINGTALK_NETWORK_A` / `DINGTALK_IDC_B` | 各供应商群机器人的 access_token（按需，名字要与 `config.yaml` 里 `${...}` 一致） |
| `DINGTALK_NETWORK_A_SECRET` / `DINGTALK_IDC_B_SECRET` | 加签密钥（仅加签模式需要） |
| `ZABBIX_SERVICE_USER` / `ZABBIX_SERVICE_PASSWORD` | **当前无效**（`auth.mode=service` 未贯通，见 10） |

> `.env.example` 是**提交进仓库**的模板，只放占位符；`.env` 已被 `.gitignore` 忽略，权限设 `600`。

### 5.3 数据库权限两种模式

| 模式 | `auto_init` | 账号权限 | 谁建表 |
|---|---|---|---|
| A（推荐，默认） | `true` | `SELECT INSERT UPDATE DELETE CREATE INDEX` | 应用启动时 `CREATE TABLE IF NOT EXISTS` |
| B（DBA 管控） | `false` | `SELECT INSERT UPDATE DELETE` | DBA 按 `deploy/schema.sql` 预建 |

两张表：`sessions`（对话历史，键是登录会话 id）、`audit_log`（钉钉发送审计）。
**插件只存自己的数据，不需要访问 Zabbix 数据库**，因此用独立库 + 独立最小权限账号。

### 5.4 LLM 配置

**仓库默认设定**（智谱官方端点，OpenAI 兼容接口）：

```yaml
llm:
  base_url: "https://open.bigmodel.cn/api/paas/v4"
  model: "glm-4-flash"
  api_key_env: "LLM_API_KEY"
  temperature: 0.3
  top_p: 0.9
  max_tokens: 1024
  function_calling: true
```

**换模型 / 换厂商**：只改 `base_url` 与 `model`，代码不用动。

| 厂商 | `base_url` | `model` 示例 |
|---|---|---|
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |

**硬性要求**：所选模型必须支持 **Function Calling**，否则工具全用不了。
另注：单次 LLM 请求超时**硬编码 60 秒**（`app/llm.py`），不可通过配置调整。

**怎么调试 LLM 连通性**：

```bash
cd /opt/zabbix-ai-plugin
sudo -u zabbix .venv/bin/python - <<'PY'
import os, httpx
key = os.environ["LLM_API_KEY"]
url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
tools = [{"type":"function","function":{
    "name":"query_problems","description":"查告警",
    "parameters":{"type":"object","properties":{"days":{"type":"integer"}},"required":["days"]}}}]

r = httpx.post(url, headers={"Authorization": f"Bearer {key}"},
               json={"model":"glm-4-flash","messages":[{"role":"user","content":"你好"}]}, timeout=60)
print("plain:", r.status_code, r.text[:200])

r = httpx.post(url, headers={"Authorization": f"Bearer {key}"},
               json={"model":"glm-4-flash","tool_choice":"auto","tools":tools,
                     "messages":[{"role":"user","content":"查一下最近3天的告警"}]}, timeout=60)
print("tools:", r.status_code, r.text[:400])
PY
```

第 2 步响应里出现 `tool_calls` 说明模型支持工具调用；只返回普通文本则需换模型。

---

## 6. 升级与日常运维

```bash
systemctl status zabbix-ai-plugin            # 状态
journalctl -u zabbix-ai-plugin -n 100 -f     # 日志
sudo systemctl restart zabbix-ai-plugin      # 重启（改 config.yaml / .env 后必须重启）
```

- 升级代码后：`sudo -u zabbix .venv/bin/pip install -r requirements.txt`（依赖有变化时）→ 重启
- 升级 Zabbix 前端**不会**影响本插件（插件没有改动 Zabbix 任何文件；唯一例外是可选的
  nginx `sub_filter` 注入，它改的是 nginx 配置而非 Zabbix 文件，可 `--uninstall`）
- 数据库表由 `CREATE TABLE IF NOT EXISTS` 维护，升级不会丢数据

**生产环境的升级/回滚/数据保留/备份流程见 `DEPLOYMENT.md` 第 11 节**
（含回滚点、`app.bak.<版本>` 备份、`history` 表无限增长的处理）。

---

## 7. 排错与常见问题

| 现象 | 原因与处理 |
|---|---|
| 页面提示"会话已失效，请先登录 zabbix" | 没有有效 `zbx_session` cookie。确认是从 dashboard 进入、且 `/ai/` 与 Zabbix 前端**同域名同端口**；跨域不会发送 cookie |
| `/api/health` 里 `mysql: false` | `.env` 的 `MYSQL_PASSWORD` 与 `schema.sql` 不一致，或 `storage.mysql.user/host` 不对，或账号 host 与连接来源不匹配 |
| 问了没反应 / 报"（服务异常）" | LLM Key 无效或额度不足。按 5.4 的脚本先验证连通性 |
| 回答总是"达到工具调用上限" | 模型 Function Calling 能力弱，换更强的模型 |
| 钉钉报 `errcode 310000` | 见 4.2 的报错表（关键词/加签/IP 白名单） |
| `/供应商名` 提示"未注册供应商" | 该名称不在 `config.yaml` 的 `suppliers` 下；名字要完全一致，或用唯一前缀 |
| `/ai/` 打开是 nginx 404 | ① 是否写成 `location ^~ /ai/`（漏 `^~` 会被 Zabbix 的 `api/` 正则拦截）；② `proxy_pass` 结尾的 `/` 不能少；③ 是否 reload 了 nginx |
| **对话框能显示，但打 `/` 没提示、点发送无反应** | **Zabbix 默认的 iframe sandbox 禁用了页面脚本**——按 3.9 设置 `Iframe sandboxing exceptions`。自检：点发送后 nginx 日志里若**完全没有** `/ai/api/*`，即是此问题 |
| 配色与 Zabbix 不一致 | 点 widget 右上角「主题」切到「跟随」；或 `?theme=light` 强制；旧版前端固定深色，需升级 |
| 端口冲突（10801 被占） | 改 **两处**：systemd 的 `ExecStart --port` 与 nginx 的 `proxy_pass`。**改 `config.yaml` 的 `server.port` 没有任何作用**（见 10） |
| 启动时报数据库权限错误 | 账号权限与 `auto_init` 不匹配：`auto_init: true` 需要 `CREATE`；只有 DML 权限则设 `auto_init: false` 并由 DBA 预建表 |
| 日志出现"表结构自检失败" | 插件启动时会校验 `sessions`/`audit_log` 结构与预期一致；按提示用 `deploy/schema.sql` 重建或修正 |

---

## 8. 开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -v
```

跑法与基线：

```bash
python -m pytest -q          # 全绿即可；用例数随版本增长，不在文档里写死
```

跳过的用例是「需要真实 MySQL」或「`dist/` 离线包不存在」的那几个——**具体数字不在文档里写死**：
每发布一个离线包都会新增参数化用例（每个包 3 个），干净 clone 与带 `dist/` 的开发机收集数也不同。

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_api.py` | 路由与鉴权（未登录 401、Zabbix 不可达 503）、`/api/chat`/`/api/history`/`/api/audit` |
| `tests/test_auth.py` | `zbx_session` 解析（URL 解码 + base64 JSON）、失效/畸形 cookie |
| `tests/test_config.py` | `config.yaml` 解析、供应商白名单与歧义前缀 |
| `tests/test_tools.py` | 5 个工具的参数校验、时段解析、key 族放宽、趋势外推（约 80 条用例） |
| `tests/test_llm.py` | LLM 请求体、工具调用循环、上限轮次 |
| `tests/test_dingtalk.py` | 加签算法、关键词注入、模板渲染 |
| `tests/test_storage_mysql.py` | MySQL 会话隔离与审计往返（需真实库，见下） |
| `tests/test_package.py` | 离线包与 `.sha256` 一致性 |
| `tests/test_shipped_config.py` | 随仓库发布的 `config.yaml` 不被改坏 |
| `tests/test_launcher_js.py` + `tests/js/*.test.js` | 前端 JS 行为自测（`node` + 最小 DOM 桩，由 pytest 驱动） |
| `tests/test_docs.py` | **文档与代码一致性**：本文出现的工具名/配置字段/环境变量/时段/文件清单必须与代码一致 |
| `tests/integration/` | 对**真实 Zabbix** 的问答验证套件（需测试服务器，见其 `README.md`） |

需要 MySQL 的用例（可选，用独立测试库）：

```bash
TEST_MYSQL_HOST=127.0.0.1 TEST_MYSQL_USER=zabbix_ai TEST_MYSQL_PASSWORD=xxx \
TEST_MYSQL_DB=zabbix_ai_plugin_test python -m pytest tests/test_storage_mysql.py -v
```

对真实服务器的问答验证（`tests/integration/`）另需：`QA_ZABBIX_API`、`QA_ZABBIX_USER`、
`QA_ZABBIX_PASSWORD`、`QA_PLUGIN`（插件地址）、`QA_SERVICE`（systemd 服务名），
环境准备与造数见 `tests/integration/README.md`。

改动约定见 `AGENTS.md`：每次改动都要提交 commit，并在交付前确保测试全部通过。

---

## 9. 目录结构

```
zabbix-ai-plugin/
├─ app/
│  ├─ __init__.py          # 包标记
│  ├─ main.py              # FastAPI 入口、路由、健康检查、斜杠命令
│  ├─ config.py            # config.yaml + 环境变量加载
│  ├─ auth.py              # zbx_session cookie 校验（401 / 503 语义）
│  ├─ zabbix.py            # Zabbix JSON-RPC 客户端（会话透传）
│  ├─ llm.py               # LLM 抽象层 + 工具调用循环
│  ├─ tools.py             # 5 个工具实现 + 注册表 + 时段解析
│  ├─ dingtalk.py          # 钉钉发送（加签 / 关键词 / 模板）
│  └─ storage.py           # MySQL：会话历史 + 审计
├─ frontend/
│  ├─ index.html           # 单页聊天界面（主题跟随、/ 命令提示、键盘优先选择）
│  └─ launcher.js          # 漂浮聊天球（nginx 注入，见 4.3）
├─ deploy/
│  ├─ schema.sql                  # 建库 / 授权 / 建表
│  ├─ nginx.conf.example          # nginx 片段示例（需复制修改，见 3.7）
│  ├─ zabbix-ai-plugin.service    # systemd 服务（端口在这里，见 3.6）
│  ├─ preflight.sh                # 部署前环境检查
│  ├─ verify.sh                   # 部署后自检
│  ├─ package.py                  # 生成生产离线包
│  └─ install_launcher.py         # 安装/卸载漂浮聊天球
├─ tests/
│  ├─ test_api.py  test_auth.py  test_config.py  test_dingtalk.py
│  ├─ test_llm.py  test_storage_mysql.py  test_package.py
│  ├─ test_shipped_config.py  test_launcher_js.py  test_tools.py
│  ├─ test_docs.py                 # 文档与代码一致性检查
│  ├─ js/                          # 前端 JS 自测（launcher.test.js、suggest.test.js）
│  └─ integration/                 # 对真实 Zabbix 的问答验证套件（qa_bank.py 题库、qa_fixture.py 造数）
├─ config.yaml             # 配置（不含密钥）
├─ .env.example            # 密钥模板 → 复制为 .env
├─ requirements.txt / requirements-dev.txt
├─ .gitignore / .gitattributes     # .gitattributes 钉死 LF（避免 .sh 在 Linux 上因 CRLF 报错）
├─ conftest.py             # pytest 公共夹具
├─ README.md               # 本文（中文）
├─ README.en.md            # English README
├─ DEPLOYMENT.md           # 生产部署文档
├─ RECOVERY-IDC.md         # IDC 现场恢复清单
├─ SECURITY.md             # 安全策略与漏洞报告
├─ CHANGELOG.md            # 版本历史
├─ LICENSE                 # GNU GPL v3.0 全文
└─ AGENTS.md               # 改动约定
```

---

## 10. 已知限制

这些是**当前版本真实存在**的限制，写在这里是为了避免"文档承诺、实际做不到"：

1. **`zabbix.auth.mode: service` 未贯通**：鉴权始终要求用户已登录 Zabbix；数据查询也始终使用
   用户会话（`ZabbixClient` 的每个方法都透传用户 `sessionid`，从不使用 service 账号）。
   因此 `ZABBIX_SERVICE_USER/PASSWORD` 读了也没用，`app/auth.py` 中"service 模式仅影响数据查询
   使用哪个账号"的注释与事实不符。**当前请只用 `user` 模式**。
2. **`server.host` / `server.port` / `server.base_path` 不生效**：这三个字段只被解析、从未被使用。
   实际监听地址与端口由启动命令决定（systemd `ExecStart` 的 `--host/--port`），路径前缀由 nginx 决定。
   排查端口冲突请改 systemd 与 nginx 两处。
3. **`llm.function_calling: false` 不是"降级"**：置 `false` 后请求里完全不发 tools，5 个工具全部不可用；
   `app/llm.py` 里的 `try_parse_tool_call()` 是**未被生产代码调用**的实验函数（仅单测覆盖），
   不存在"提示词式解析"的兜底路径。
4. **「昨天夜里」解析错误**：中文口语词表里 `夜里`（今天 18:00-24:00）先于日期词匹配，
   所以"昨天夜里"会被当成**今天** 18:00-24:00。当前请说"昨晚"。
   同理"上月"目前不被中文口语词表识别（用"本月"或让模型映射 `last_month`）。
5. **中文时长词不被服务端解析**："最近7天""最近1小时"这类说法依赖**模型**映射成 `last_7d` / `last_1h`；
   若模型把中文原样传进工具参数，插件会明确回答"未能识别时段"并按最近 1 天查询（不会假装是你要的窗口）。
6. **供应商候选最多显示 10 个**：`config.yaml` 可以配任意多个供应商，但输入 `/` 时前端只列前 10 个匹配项；
   更多时请继续输入前缀缩小范围。
7. **LLM 请求超时硬编码 60 秒**，不可配置；大模型长时间无响应时表现为等待后失败。
8. **`GET /` 对未登录请求也返回 200**：页面本身不鉴权，靠前端调用 `/api/*` 拿到 401 后提示"会话已失效"。
   真正的数据接口一律 401，不存在未授权取数。
9. **`zabbix.frontend_url` 仅展示用**：代码不读它做任何判断，改它不会影响访问路径。

> 如果你希望上面某条从"已知限制"变成"已修复"，那就是一次代码改动（需要发新版 + 部署），
> 建议先在 `tests/` 里补一条能复现的用例再动手。

---

## 许可证

本项目以 **GNU General Public License v3.0** 发布，全文见 [`LICENSE`](LICENSE)；
界面右上角的「**关于**」入口也能直接查看（`GET /license`，无需登录）。

Copyright (C) 2026 **shourenli**。

- 你可以自由地使用、修改、再分发本项目，包括用于商业用途；
- 但**分发修改版或衍生作品时，必须同样以 GPL-3.0 开源**（copyleft），并提供对应源码；
- 软件按"现状"提供，不含任何担保。

如需在闭源产品中集成，或需要其它授权方式，请联系仓库所有者另行协商。

> 说明：GPL-3.0 覆盖的是本仓库中的源码、脚本与文档。`.env`、密钥、生产配置与运行数据
> 不属于本仓库，也不随本项目分发（见 [`SECURITY.md`](SECURITY.md)）。
