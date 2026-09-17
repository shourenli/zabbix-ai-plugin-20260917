# 变更日志（CHANGELOG）

**Zabbix AI 对话框插件**（`zabbix-ai-plugin`，仓库目录 `zabbix-dashboard-plugin`）的版本变更记录。

它在 Zabbix dashboard 里嵌入一个 AI 对话框：FastAPI 后端 + 原生 JS 前端，经 nginx 反代到与 Zabbix
**同源**的 `/ai/`，由 dashboard 的 URL widget 以 iframe 嵌入。用自然语言查告警、查指标、做趋势外推，
并用 `/供应商名 消息内容` 把消息发到对应供应商的钉钉群。生产部署见 [`DEPLOYMENT.md`](DEPLOYMENT.md)，
安全边界与已知限制见 [`SECURITY.md`](SECURITY.md)。

## 版本号规则

版本号形如 `vMAJOR.MINOR.PATCH`，tag 名与之完全一致。以下不是规范引用，而是按 10 个 tag 的**实际历史**归纳：

| 位置 | 本仓库取值 | 实际用法 |
|---|---|---|
| `MAJOR` | 恒为 `1` | 对应「首个生产可用版本」`v1.0.0`。只有做不兼容的大改（推翻接口或存储结构）才该递增 |
| `MINOR` | `0` → `9` | **每发布一版递增一次**，表示「相对上一个 tag 有用户可见的变化」 |
| `PATCH` | 恒为 `0` | 从未发布过补丁版本；本项目连纯修复也走 `MINOR` |

> ⚠️ 因此本项目**不是**严格的语义化版本：`v1.0.3`（修「告警无主机名」、收回模型的发送能力）、
> `v1.0.4`、`v1.0.6`、`v1.0.9` 都是以修复为主的版本，却都递增了 `MINOR`。
> 只看版本号判断不出升级是否含新功能，请以本文件的分类为准。
> 补充说明：`DEPLOYMENT.md`、`README.md`、`SECURITY.md` 里**都没有写明**这条规则，上表是按 tag 历史归纳的。

## 版本与 git tag

- 本仓库自 `v1.1.0` 起为**重建的干净仓库**：`main` 分支、单次初始提交，历史中不含任何真实凭据
  或真实资产标识。`v1.0.0` – `v1.0.9` 的 10 个标签属于**已删除的旧仓库**（其历史含一把已泄露的
  LLM API Key），本仓库不复刻它们，但保留它们的变更记录（见下方各版本条目）。
- 下方 `v1.0.0` – `v1.0.9` 条目里引用的提交哈希属于旧仓库，**在本仓库无法解析**，仅作追溯线索；
  引用的 `文件:行号` 以本仓库当前 `HEAD` 为准。

---

## v1.1.0 — 2026-09-17

> 重建后的首个发布版本：清除了全部真实资产标识与凭据，并加固了交付链路。
> 处置对象是第三方安全审计报告（含 1 Critical / 4 High / 10 Medium / 8 Low）中与
> **敏感信息泄露**相关的部分；其余代码级安全项不在本版本范围内。

### 安全

> ⚠️ 安全
>
> - **仓库与离线包中不再存在任何真实密钥。** 仓库 `.env.example` 只放占位符；
>   **离线包里的 `.env.example` 改为打包时现场生成**（常量内容、所有值为空），不再从仓库复制——
>   即使仓库里的模板将来被误填真实值，也不会随包分发（审计 C-1 的结构性修复）。
> - **打包新增内容级密钥检查**：命中 LLM Key 形态（`<32 位 hex>.<16 位>`）、`sk-` 开头、
>   钉钉 `access_token=<明文>`、私钥内容，或模板值不是占位符，即中止打包并指出文件。
>   用历史泄露样本验证，可拦下全部 9 个曾泄露的旧离线包。
> - **清除全部真实资产标识**（审计 M-5）：文档、代码注释与测试数据中的真实主机名（含带 WAN
>   后缀的变体）、真实公网 IP、组织名与运营商名，统一替换为合成值（`DEMO_*` / `AIQA_*` 前缀、
>   RFC 5737 文档保留地址、`SUPPLIER_A` 等）。
> - **新增全仓守卫测试** `tests/test_docs.py::test_repo_contains_no_real_assets_or_secrets`：
>   扫描真实主机名形态、非保留网段 IPv4、密钥形态；真实名称以**词哈希**形式存入守卫，
>   仓库本身不再出现明文，命中即测试失败。
> - **`SECURITY.md` 写明历史密钥的处置要求**：旧仓库 10 个标签中的那把 Key 必须到服务商处
>   吊销并重新签发——删除仓库、重写历史都无法收回已扩散的副本。

### 变更

- **README 重写**为使用者/运维手册：修正架构图里的工具数与「按登录会话隔离」的口径、
  补齐 14 个时段键与配置字段全表、新增「已知限制」一节（含 `auth.mode=service` 尚未贯通、
  `server.host/port/base_path` 不生效等已被代码验证的事实）。
- **新增 `CHANGELOG.md`**（本文件）与 `tests/test_docs.py`：文档里出现的工具名/配置字段/
  环境变量/时段键/目录树/版本号/内部锚点都要与代码一致，否则测试失败。
- **`DEPLOYMENT.md` 事实性修正**：交付物清单与离线包实际内容对齐、`verify.sh` 调用补 `BASE_URL`
  （原样会必然失败）、参考环境端口统一为 801、离线包文件名改为占位符（产物带 `-N-g<短哈希>` 后缀）、
  不再指引 `git checkout` 含已泄露密钥的历史标签；原「附录 A：版本演进中修复的坑」的 20 条踩坑表
  并入本文件各版本条目。
- **打包工具**：文件收集跳过 `__pycache__`/`.pytest_cache`（此前只要跑过一次 `pytest`，
  打包就会因收集到 `.pyc` 而中止）。
- **新增 `.gitattributes`**（`* text=auto eol=lf`）：避免 Windows 开发机检出成 CRLF 后
  `deploy/*.sh` 在 Linux 上报 `bad interpreter`。
- **明确许可证：GNU GPL v3.0**（新增 `LICENSE`，文本与 GitHub 官方 GPL-3.0 逐字一致）。
  离线包一并携带该文件（包内文件数 24 → 26）。
- **文档中英双语**：`README.md`（中文，主）与 `README.en.md`（英文），顶部互相跳转。
- **GPL-3.0 界面通知**：前端右上角新增「**关于**」入口，展示版权与许可证，
  并提供 `GET /license`（**无需登录**）查看全文——符合 GPL-3.0 §5(d) 对交互界面展示
  Appropriate Legal Notices 的惯例；版权声明为 `Copyright (C) 2026 shourenli`。
- **修正 `config.yaml` 的一处错误注释**：原文称 `function_calling: false` 会"退化为提示词式
  工具解析"，与代码不符（`false` 时请求里根本不发 tools，5 个工具全部不可用）。
- **文档口径修正**：README 4.4 不再称接口挂在 `server.base_path` 下（该字段不生效，
  前缀由 nginx 决定）、第 8 节不再写死 skip 用例数、文档地图的版本范围更新到 v1.1.0。
- 回归测试：`python -m pytest`（用例数不写死；每发布一个离线包会新增参数化用例，写死必然过期）。

---

## v1.0.9 — 2026-09-16

> tag `v1.0.9`（轻量 tag，指向 `94b3e16`）· 2 个提交（`1c91d40`、`94b3e16`）
> · 影响 4 个文件：`frontend/index.html`、`tests/js/suggest.test.js`、`README.md`、`DEPLOYMENT.md`

### 修复

- **只命中一个供应商时，↑/↓ 不再被吞掉**：有用户追问「上下箭头是不是必须两个以上供应商才能用」——
  只有一个候选时下标恒为 0（选择没有意义，看起来像「没反应」），但旧实现仍无条件 `preventDefault()`，
  把方向键吃掉了，输入内容换行时光标无法上下移动，很难受。现在**仅当候选数 ≥ 2 才接管 ↑/↓**，
  单候选时交给输入框自己处理（`frontend/index.html:334` 的 `suggestItems.length > 1`）；
  单候选下 `Enter`/`Tab` 仍然直接补全（`frontend/index.html:339`）。commit `94b3e16`。
- 自测补 6 项断言，含「单候选时方向键不得被 `preventDefault`」（`tests/js/suggest.test.js`）。

### 变更

- 文档里的回归测试数不再写死：说明其中一部分是随 `dist/` 里离线包数量增长的参数化用例（commit `1c91d40`）。

---

## v1.0.8 — 2026-09-16

> tag `v1.0.8`（轻量 tag，指向 `aa8e896`）· 1 个提交
> · 影响 5 个文件：`frontend/index.html`、`tests/js/suggest.test.js`（新增）、`tests/test_launcher_js.py`、`README.md`、`DEPLOYMENT.md`

### 新增

- **`/供应商名` 建议列表改为键盘优先**：用户反馈「打 `/` 会弹出供应商列表，但只能用鼠标点，每次都要把手
  挪到鼠标」。现在打 `/` 后**默认高亮第一项**，`↑`/`↓` 移动（到头循环），`Enter` 或 `Tab` 补全，`Esc` 收起；
  鼠标点击仍然可用（`frontend/index.html:202` `updateSuggest()`、`:242` `moveSuggest()`、`:348` `Escape`）。
  commit `aa8e896`。
- 候选项设 `tabIndex=-1`，不再抢走 `Tab` 焦点（`frontend/index.html:216`）。
- 新增前端行为自测 `tests/js/suggest.test.js`（node + 最小 DOM 桩，由 pytest 参数化执行）。

### 变更

- 关键约束：**已经输入空格（开始写正文）后 `Enter` 必须恢复为「发送」**，否则会给老用户造成
  「按回车发不出去」的回归；该行为已用自测锁住（`frontend/index.html:347`）。

---

## v1.0.7 — 2026-09-16

> tag `v1.0.7`（轻量 tag，指向 `015b07d`）· 1 个提交
> · 影响 13 个文件，含 `app/storage.py`、`app/main.py`、`app/config.py`、`config.yaml`、`deploy/schema.sql`、
> `frontend/index.html`、`frontend/launcher.js`

### 新增

- **对话按「登录会话」隔离**：用户反馈「注销后重登，小助手里的对话还在」。根因是对话按 zabbix `userid`
  存储，换一次登录当然是同一份。已实测确认 Zabbix 每次登录都下发新的 `sessionid`、同一会话内稳定、
  注销即失效，因此把对话绑定到 `sessionid`，**结构性保证**「重登即新会话」，不依赖前端能否捕获注销动作
  （`app/main.py:183` `_session_key()`；`app/storage.py:186` `get_context()`）。
  同一账号开两个浏览器也各自独立。
- **闲置自动开新会话**：最后一条消息距今超过 `history.ttl_minutes`（默认 15）时，下次打开或提问自动开始
  新会话并删除旧记录；前端在 `context_reset` 时清空界面并提示，避免「看到的」与「模型记得的」不一致
  （`app/main.py:192` `_history_ttl_seconds()`、`config.yaml` `history` 段）。
- **启动时兜底清理**：删除超过 `history.retention_days`（默认 7 天）的历史记录（`app/storage.py:234` `purge_expired()`）。
- 漂浮球在点注销时用 `sendBeacon` 尽力立刻清空服务端记录（任何页面都生效，`frontend/launcher.js:25`）。

### 修复

- **同一句提问在模型上下文里出现两遍**：既写库又追加，现只保留一份。
- **存储不可用不再静默**：新增启动只读自检 `check_schema()`，存储不可用时启动日志里会留下 `ERROR`，
  不再等到用户提问才暴露（`app/storage.py:130`）。

### 变更

- **关键取舍：不加新列**。原方案给 `sessions` 加 `session_key`，但运行期账号按 `deploy/schema.sql`
  只有 `SELECT/INSERT/UPDATE/DELETE/CREATE/INDEX`，**没有 `ALTER`**——实测报 `1142`，且失败被当成 warning
  吞掉，服务照常启动、每次对话 502。改为**复用既有的 `user_id` 列**存会话键：零 DDL、零权限要求，
  新老安装升级只换代码（`deploy/schema.sql`、`app/storage.py`）。

---

## v1.0.6 — 2026-09-16

> tag `v1.0.6`（轻量 tag，指向 `9d1c255`）· 1 个提交
> · 影响 6 个文件：`app/tools.py`、`app/main.py`、`tests/test_tools.py`、`tests/integration/qa_bank.py`、`README.md`、`DEPLOYMENT.md`

### 修复

- **「中午有告警吗」不再答成跨 24 小时的告警**：生产实测里模型**传对了** `period=noon`（它没错），
  但服务端没有这个时段名，`resolve_period()` 直接落到「最近 N 天」兜底，返回的描述里也看不出来——
  模型于是把跨 24 小时、含前一天的事件答成了「中午的告警」。现在补齐
  `noon`(11:00-14:00) / `early_morning`(0-6) / `evening`(18-24) / `last_night`，下午与晚上不再互相吞并
  （`app/tools.py:199` `resolve_period()`；`app/tools.py:154` `_parse_cn_period()`）。
- **认不出来的时段绝不静默替换**：窗口描述与 `warning` 里明确标注，并给出受支持列表
  （`app/tools.py:189` `period_supported()`）。
- `query_problems` 增加 `truncated` 提示，避免「前 N 条」被当成全量（`app/tools.py:591`）。
- 提示词：回答带时间的提问必须先说明实际查询范围（`app/main.py`）。

### 变更

- 新增中文口语时段解析：中午 / 凌晨 / 上午 / 下午 / 傍晚 / 晚上 / 昨晚，并支持「昨天下午」这类组合。
- 验证：pytest 187 passed；测试机问题库 18/18（新增「中午」「凌晨」两题，ground truth 按事件发生时间
  独立切窗，测试机上那 4 条 10:44 的告警被正确排除在中午窗口之外）。

---

## v1.0.5 — 2026-09-16

> tag `v1.0.5`（轻量 tag，指向 `e3c5f5b`）· 2 个提交（`b957f53` 功能、`e3c5f5b` 文档）
> · 影响 8 个文件，含 `deploy/install_launcher.py`（新增）、`frontend/launcher.js`（新增）、
> `tests/js/launcher.test.js`（新增）、`app/main.py`、`frontend/index.html`

### 新增

- **dashboard 右下角漂浮聊天球**：不想让对话框占满 dashboard 网格时，可点右下角小球弹出小面板内嵌对话，
  **不跳转、不刷新**；小球可拖动，位置记在 `localStorage`。实现方式是在 nginx 的 PHP location 里加 3 行
  `sub_filter`，把 `<script src="/ai/launcher.js" defer>` 注入到每个 HTML 页面的 `</body>` 前，
  **Zabbix 自身文件一个都不改**（因此 `apt upgrade zabbix-frontend-php` 不会覆盖它），
  `--uninstall` 可撤销（`deploy/install_launcher.py`、`frontend/launcher.js`）。commit `b957f53`。
- 漂浮球只在 dashboard 页面且已登录时出现：`launcher.js` 校验 URL 里的 `action=dashboard.view` 与页面上的
  个人资料入口，登录页、告警列表等页面一颗球都不会创建。
- 新增前端浮球自测 `tests/js/launcher.test.js`（node + 最小 DOM 桩，由 pytest 一并执行）。

### 修复

- **漂浮球整段脚本加载失败、小球完全不出现**：关闭按钮的变量名与关闭函数重名（都叫 `close`），
  严格模式下整段脚本报错；而该脚本刻意吞掉所有异常（避免影响 Zabbix 页面），浏览器里没有任何报错线索。
  现改为 `closeBtn` / `close()`（`frontend/launcher.js:135`、`:179`）。
- **安装器在 `systemctl reload nginx` 之后立即自检，误报「注入没生效」**：reload 是**优雅重载**，
  旧 worker 会继续服务一段时间，立刻请求可能仍由旧配置应答。现改为重试数次再下结论。

---

## v1.0.4 — 2026-09-16

> tag `v1.0.4`（轻量 tag，指向 `d88db36`）· 1 个提交 · 影响 9 个文件（`+1379 / -189`），
> 主改 `app/tools.py`、`app/zabbix.py`、`tests/test_tools.py`

### 修复

- **「DEMO_SDWAN01 本月运行状况分析」这类问题都答「无法获取数据」**：生产实测日志显示模型连猜 3 个 key
  （`system.cpu.load[0,5,15min]`、`net.if.incoming.bytes`、`net.if.outgoing.bytes`）全部落空——那两台是
  **SNMP 设备**（`interfaces.type=2`），真实 key 是 `net.if.in[ifHCInOctets.15]`；它们实际各有 198 / 188 个
  监控项、近 30 天趋势两万多行。根因不是「没有数据」，而是工具在 key 未命中时只回 `{"items": []}`、
  不给任何线索。现在未命中时**回传该主机真实存在的 key 族清单**（含单位与样本 key），
  支持前缀放宽（`net.if.incoming.bytes` → `net.if.in`），搜索同时匹配 key 与监控项名称
  （`app/tools.py:777` `tool_query_metrics()`、`:291` `_key_family()`、`:331` `_key_families()`、
  `:367` `_relax_key_hint()`）。commit `d88db36`。
- **「本月运行状况」抽到禁用项就报「数据不足」**：`trend_analysis` 旧实现取 `items[:5]` 当候选，
  而生产上这两台主机约**一半监控项是禁用的**（113/198、95/188），另有 LLD 生成项与原型，必然抽到
  没有数据的项。现按「已启用 + 数值型 + 有采集数据」筛选排序，并排除 LLD 规则与原型
  （`app/tools.py:924` `tool_trend_analysis()`、`:307` `_usable_items()`）。

### 新增

- `query_problems` 新增 `host` 参数：以前答不了「某台主机本月有哪些故障」「告警那个时间段的带宽是多少」。
  走 `problem.get` / `event.get` 的 `hostids`（已在 Zabbix `CProblem.php` 源码核对确为真支持）
  （`app/tools.py:520` `tool_query_problems()`、`:503` `_resolve_hostids()`、`:484` `_pick_host()`）。
- `query_metrics` 新增 `period`：返回该时段的 min/max/avg/first/last，此前只有 `lastvalue`
  （`app/tools.py:446` `_window_stats()`）。
- 新增 SNMP 主机 fixture（SNMP 风格 key + 文本项 + 禁用项），在测试里复现上述生产场景；
  集成问题库由 11 题扩到 16 题。

### 变更

- `items_by_host` 上限 50 → 300：单台 SNMP 设备 150+ 个监控项会被截断；并补 `status` / `flags` / `lastclock`。
- 提示词：禁止凭印象猜 key；工具返回 `relaxed_from` 时必须说明原 key 不存在；禁止编造工具名与工具调用结果
  （`app/tools.py:914`）。
- 顺带修掉验证工具自身的三个坑：SNMP 接口必须带 `details`、前置数据缺失会「空转通过」、
  新建监控项未进 Zabbix 配置缓存导致发送被丢弃（`processed: 0; failed: 1`）。
- 验证：单元测试 158 → 173 passed / 8 skipped；测试机问题库 16/16 通过。

---

## v1.0.3 — 2026-09-15

> tag `v1.0.3`（带注释 tag，指向 `657b7e1`）· 1 个提交 · 影响 11 个文件（`+934 / -33`），
> 含 `app/tools.py`、`app/zabbix.py`、`app/main.py`、`tests/integration/`（新增）

### 修复

- **「现在有哪些告警」永远答不出是哪台主机在报警**：`problem.get` **不支持 `selectHosts`**——
  Zabbix 的 `CProblem.php` 只认 `selectAcknowledges` / `selectSuppressionData` / `selectTags`，
  传 `selectHosts` 会被**静默忽略**（不报错、也不返回 hosts）。现改为用 `objectid`(=triggerid) 经
  `trigger.get` 反查主机名（`app/zabbix.py`、`app/tools.py:567`）。

> ⚠️ 安全
>
> - **`send_dingtalk` 不再暴露给 LLM**：有用户问「今天上海的天气怎么样」，模型把这条**真的转发到了供应商
>   钉钉群**。发送是有真实外部副作用的动作，交给模型自主决定不可靠（另一面是「没调工具却答已发送」）。
>   现从 LLM 工具表**移除**，发送只能由 `/供应商名 消息内容` 触发、走确定性代码路径
>   （`app/tools.py:1192` `build_tool_schemas()` 已不含 `send_dingtalk`；`app/tools.py:1335`
>   `build_tool_handlers()` 里有注释说明原因；`app/main.py:211` `_handle_slash_command()`、
>   `:229` 调用 `tool_send_dingtalk()`）。commit `657b7e1`。

### 新增

- **问答验证套件 `tests/integration/`**：用**真实大模型**跑一份问题库，期望值由 Zabbix API **独立计算**
  （而非写死），同时从日志断言「实际调用了哪个工具、传了什么参数」（`qa_bank.py`、`qa_fixture.py`、
  `README.md`）。这是后来 v1.0.4 / v1.0.6 几条修复的发现手段。

### 变更

- `deploy/package.py` 与 `tests/test_package.py` 同步扩展（离线包统计与断言）。

---

## v1.0.2 — 2026-09-15

> tag `v1.0.2`（带注释 tag，指向 `73c0b3b`）· **8 个提交** · 影响 9 个文件（`+1212 / -45`），
> 含 `app/tools.py`、`app/zabbix.py`、`app/config.py`、`config.yaml`
>
> 这一版的 tag 注释概括为「query_hosts 支持返回主机 IP/端口；含 v1.0.1 以来的 query_top、
> 已恢复故障、`.env` 加载、钉钉模板等修复」。

### 新增

- **`query_hosts` 返回主机 IP/端口**：问「某台主机的 IP 地址」以前永远答「无法查询」，根因是
  `host.get` 未请求 `selectInterfaces`，模型手里从来没有 IP 字段——**模型如实回答而非编造，属正确行为；
  缺的是能力不是模型**。现返回 `ip` / `port` / 接口类型；`useip=0` 的主机返回 DNS 名，
  无接口主机（如 Zabbix server 自身）如实说明（`app/tools.py:1120` `tool_query_hosts()`、
  `:1104` `_pick_interface()`）。commit `c85512d`。
- **新增 `query_top` 做服务端聚合排名**：修掉「TOP10 统计错误」——旧实现取回事件后在本地统计，
  结果不对；现在服务端聚合该时段**全部**事件（含已恢复）后按主机/触发器排名
  （`app/tools.py:697` `tool_query_top()`）。commit `c9b0c87`。
- **钉钉消息正文改为可配置模板**：默认值不变（`[zabbix-AI] {username} 上报：\n{message}`），
  支持全局与供应商级覆盖（`app/tools.py:43` `render_message()`；`config.yaml` `dingtalk.message_template`）。
  commit `4543d24`。

### 修复

- **查不到已恢复的故障**：`query_problems` 只返回当前告警，答不了「今天上午有什么故障」。现在时间段查询
  **一律包含已恢复故障**，不再依赖模型选对 `scope`（commit `c4cebd4`、`25284e8`）。
- **`.env` 读取失败导致启动崩溃**：`.env` 存在但当前用户读不到时（权限 `600` 且非属主），
  `load_dotenv` 抛 `PermissionError`，整个应用起不来，且报错与「密钥没配好」难以区分。现捕获异常、
  记 warning 并降级为「只用进程已有的环境变量」（`app/config.py` `load_env_file()`）。commit `f9b2156`。

### 变更

- 文档补充 MySQL 只监听特定 IP 时的部署要点（生产实测），以及离线包首次启动 `mysql:false` 属正常
  （commit `1cce89b`、`0ab63d4`）。

---

## v1.0.1 — 2026-09-15

> tag `v1.0.1`（带注释 tag，指向 `d2a0db6`）· 4 个提交 · 影响 5 个文件（`+760 / -15`）
>
> tag 注释明确写着：**运行时代码未变，与 v1.0.0 行为一致；测试 114 passed**。

### 新增

- **生产离线包打包工具 `deploy/package.py`**：白名单收集，生成 `zabbix-ai-plugin-<version>.tar.gz` +
  `.sha256`，包内 `MANIFEST.txt` 记录版本 / commit / 构建时间 / 逐文件 SHA256。commit `4e05d90`。
- **IDC 现场恢复清单 [`RECOVERY-IDC.md`](RECOVERY-IDC.md)**：适用物理机 / SSH 已连不上的场景——
  出发前准备、到场先看什么、系统起不来时的 U 盘 Live 救援、恢复后验证。commit `d2a0db6`。

> ⚠️ 安全
>
> - 离线包用**白名单 + 禁止内容断言**两道闸：`.env` / `.venv` / `__pycache__` / `.git` **不可能入包**
>   （`deploy/package.py`）。这是本项目第一次把「密钥不进交付物」写成可执行的检查。
>   （但同一时期 `.env.example` 里仍留着一把真实密钥——见文首「未发版变更」。）

### 变更

- `DEPLOYMENT.md` 新增 5.0 节离线包部署流程（生成 / 校验 / 解压 / 升级注意）。
- **生产环境依赖安装改为安全做法**：不再在生产机上直接 `apt install`，另新增「附录 B：apt 操作后
  生产异常的恢复」。commit `87c5d8a`。
- 同步收紧了 `.gitignore`。

---

## v1.0.0 — 2026-09-15

> tag `v1.0.0`（带注释 tag，指向 `e0a386b`）· 13 个提交（`0bd749b` … `e0a386b`）
> · 发布提交只改文档，运行时代码在此前的 12 个提交里
>
> **首个生产可用版本。** tag 注释记录：已在参考环境完整验证——Ubuntu 24.04.4 + Zabbix 6.0.48（前端 801）
> + MySQL 8.0.46 + nginx 1.24.0，含真实钉钉发送成功；测试 114 passed。

### 新增

- **可部署的完整插件**：FastAPI 后端 + 原生 JS 前端，经 nginx 反代到与 Zabbix 同源的 `/ai/`，
  由 dashboard 的 URL widget 以 iframe 嵌入。
- **存储**：MySQL 8.0 独立库 + 最小权限账号，`auto_init` 支持 DBA 预建表或应用自动建表
  （`deploy/schema.sql`）。插件不读 Zabbix 的数据库表。
- **鉴权**：`zbx_session` 会话透传，未登录 **401**、Zabbix 不可达 **503**（刻意区分，503 不会被误读成
  「你没登录」）；每个用户的可见范围等同于他在 Zabbix 里的权限。
- **工具（当时 5 个）**：`query_problems` / `query_metrics` / `query_hosts` / `trend_analysis` /
  `send_dingtalk`。
- **钉钉**：支持群机器人自定义关键词与**加签**（`app/dingtalk.py`）。
- **前端**：亮/暗配色跟随 Zabbix；输入 `/` 自动提示供应商。
- **文档与脚本**：[`DEPLOYMENT.md`](DEPLOYMENT.md)（生产部署，630 行）、`deploy/preflight.sh`、
  `deploy/verify.sh`。

### 修复

发布前在真实部署 / 联调中暴露并修掉的问题（原 `DEPLOYMENT.md` 附录 A 的 #1–#9，该表已并入本文件）：

- **「查告警必然报 `-32500 Sorting by field "clock" not allowed`」**：`problem.get` 只允许按 `eventid`
  排序。commit `904a77c`。
- **「`/供应商名` 任何情况下都发不出去」**：工具 `send_dingtalk` 曾是 `async`，而工具调用链是同步的，
  handler 返回 coroutine 导致序列化失败。commit `904a77c`。
- **「手工启动时 `.env` 未加载 → `Illegal header value b'Bearer '`」**：装了 python-dotenv 却从未调用；
  现改为启动时主动加载，保证 systemd 与手工启动行为一致（`app/config.py`）。commit `904a77c`。
- **「模型编造『已发送』」**：未调用工具却回答已发送成功。LLM 是否调用工具取决于上下文、不可靠；
  现改为**斜杠命令确定性执行**，保证「报告成功」等同于「确实发送成功」（`app/main.py`）。commit `9f8a03c`。
- **「新装环境趋势分析不可用，报数据不足」**：Zabbix 按整点写趋势表，趋势表为空就报「数据不足」；
  现回退到 `history.get` 估算。commit `904a77c`。
- **「永远提示会话已过期，重新登录无效」**：PHP `setcookie()` 会 URL 编码（`=` → `%3D`），PHP 读取时
  自动解码而我们没有，导致 `zbx_session` 解析永远失败。现对 cookie 做 URL 解码（`app/auth.py`）。
  commit `4d4364c`。
- **「页面能显示但 JS 完全不执行」**：Zabbix 默认 `iframe_sandboxing_enabled=1` 且例外字段为空，
  iframe 变成 `sandbox=""`，脚本 / 同源 / Cookie 全被禁用。commit `0d8a4d0`。
- **「`/ai/*` 全部 404」**：Zabbix 自带的 `location ~ /(api\/|conf[^\.]|include|locale) { deny all; return 404; }`
  会命中 `/ai/api/...`，必须写成 `location ^~ /ai/`（`^~` 让前缀匹配跳过正则检查）。commit `4d36fc5`。
- **「`python3 -m venv` 报 `ensurepip is not available`」**：Ubuntu 默认不带 `python3-venv`。commit `4d36fc5`。

### 变更（当时的状态与能力边界）

- **`send_dingtalk` 当时是注册给模型的工具**——`app/tools.py` 的 `build_tool_schemas()` 里含
  `send_dingtalk`，即**模型可以自己决定往供应商钉钉群发消息**。这个能力在 **v1.0.3** 才被收掉
  （理由见 v1.0.3 的安全条目）。所以「模型自主外发」是 v1.0.0 – v1.0.2 三个版本真实存在的行为，
  不是后来才引入的回归。
- `query_top` 尚不存在（v1.0.2 新增）；`query_hosts` 不返回 IP / 端口（v1.0.2 补齐）；
  `query_problems` 没有 `host` 过滤、`query_metrics` 没有 `period`（v1.0.4 补齐）。
- 对话历史按 zabbix `userid` 存储，**注销重登后历史仍在**（v1.0.7 改为按登录会话隔离）。
- 钉钉消息正文格式固定，不可配置（v1.0.2 改为模板）。
- 无离线包工具，`dist/` 里没有 `v1.0.0` 的包（打包工具 v1.0.1 才出现）。
- 鉴权与数据查询始终使用提问者自己的会话，`auth.mode: service` 未贯通。

> ⚠️ 安全
>
> - **本版（以及 v1.0.1 – v1.0.9）的 `.env.example` 里含一把真实可用的 LLM API Key**，
>   引入于提交 `48469f3`（早于 v1.0.0）。直到 `v1.0.9` 之后的 `56dbf08` 才清空，**该修复至今未发版**。
>   详见 [`SECURITY.md`](SECURITY.md) 与本文「未发版变更」。
> - 为让插件在 dashboard 里可用，`DEPLOYMENT.md` 6.1 要求把 Zabbix 的
>   `Iframe sandboxing exceptions` 填成 `allow-scripts allow-same-origin`。这是**有意为之的安全取舍**：
>   该设置对**所有** URL widget 全局生效，若还有其它 URL widget 承载不可信外部内容，需另行评估风险。
