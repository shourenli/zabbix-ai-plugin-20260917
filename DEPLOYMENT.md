# Zabbix AI 对话框插件 —— 生产部署文档

> 本文档面向**在真实生产环境部署**。所有步骤均已在参考环境完整验证，并附带
> 两个可执行脚本：`deploy/preflight.sh`（部署前检查）、`deploy/verify.sh`（部署后自检）。
>
> 项目概览与开发说明见 [`README.md`](README.md)。

| 项 | 值 |
|---|---|
| 版本 | **v1.1.0**（`git describe --tags` 查看；历史与变更见 [`CHANGELOG.md`](CHANGELOG.md)） |
| 适用 Zabbix | 6.0 LTS / 7.0 |
| 目标系统 | Ubuntu 22.04 / 24.04 |
| 存储 | MySQL 8.0（独立库，**不使用 Zabbix 的表**） |
| 运行时 | Python 3.10+ / FastAPI / uvicorn，监听 `127.0.0.1:10801` |
| 已验证参考环境 | Ubuntu 24.04.4 + Zabbix 6.0.48（前端 801）+ MySQL 8.0.46 + nginx 1.24.0 |
| 离线包 | `zabbix-ai-plugin-<版本>.tar.gz`，约 100 KB，见 [5.0](#50-获取部署文件离线包推荐) |

---

## 目录

1. [交付物清单](#1-交付物清单)
2. [架构与数据流](#2-架构与数据流)
3. [环境要求](#3-环境要求)（含 [3.1 依赖安装的安全做法](#31-依赖安装的安全做法务必先读)）
4. [部署前检查](#4-部署前检查)
5. [部署步骤](#5-部署步骤)
6. [Zabbix 侧必做设置](#6-zabbix-侧必做设置)
7. [配置参考](#7-配置参考)
8. [部署后自检](#8-部署后自检)
9. [浏览器验收](#9-浏览器验收)
10. [生产加固清单](#10-生产加固清单)
11. [运维：日志 / 升级 / 回滚 / 数据保留](#11-运维日志--升级--回滚--数据保留)
12. [故障排查](#12-故障排查)
13. [附录 A：历史踩坑与版本变更](#附录-a历史踩坑与版本变更)
14. [附录 B：apt 操作后生产异常的恢复](#附录-bapt-操作后生产异常的恢复)

---

## 1. 交付物清单

```
zabbix-dashboard-plugin/
├─ app/                          # 后端（FastAPI）
│  ├─ __init__.py                # 包标记
│  ├─ main.py                    # 入口、路由、健康检查、斜杠命令确定性执行
│  ├─ config.py                  # config.yaml + .env 加载
│  ├─ auth.py                    # zbx_session 校验（含 URL 解码）
│  ├─ zabbix.py                  # Zabbix JSON-RPC 客户端（会话透传）
│  ├─ llm.py                     # LLM 抽象层 + 工具调用循环
│  ├─ tools.py                   # 5 个工具 + 白名单 + 审计
│  ├─ dingtalk.py                # 钉钉发送（关键词 / 加签）
│  └─ storage.py                 # MySQL：会话历史 + 审计
├─ frontend/
│  ├─ index.html                 # 单页聊天界面（亮/暗主题、/ 命令提示）
│  └─ launcher.js                # 漂浮聊天球脚本（可选功能，nginx 注入）
├─ deploy/
│  ├─ schema.sql                 # 建库 / 授权 / 建表
│  ├─ nginx.conf.example         # nginx location 片段（需合并，勿直接使用）
│  ├─ zabbix-ai-plugin.service   # systemd 单元（监听端口在这里）
│  ├─ preflight.sh               # 部署前环境检查
│  ├─ verify.sh                  # 部署后自检
│  ├─ install_launcher.py        # 安装/卸载漂浮聊天球（可选）
│  └─ package.py                 # 生成生产离线包
├─ config.yaml                   # 配置（不含密钥）
├─ .env.example                  # 密钥模板（**包内由打包脚本现场生成，所有值为空**）
├─ requirements.txt
├─ README.md / README.en.md / DEPLOYMENT.md / RECOVERY-IDC.md / LICENSE
```

**生产环境只需要其中 26 个文件**（见 [5.0](#50-获取部署文件离线包推荐) 的离线包清单）；
`tests/`、`conftest.py`、`requirements-dev.txt`、`AGENTS.md`、`SECURITY.md`、`CHANGELOG.md`、
`.gitignore`、`.gitattributes` 属仓库内容，**不进离线包**。

**获取部署文件**：推荐用离线包（见 [5.0](#50-获取部署文件离线包推荐)），它按白名单打包、
`.env.example` 里只有占位符。也可从 git 拉默认分支：

> ⚠️ **不要 `git checkout` 历史标签**：`v1.0.0`–`v1.0.9` 各标签的 `.env.example` 中含一把
> 真实 LLM Key（已在后续提交中清空，但历史提交里仍在；请按 [`SECURITY.md`](SECURITY.md)
> 处理并轮换该 Key），且旧标签没有 `SECURITY.md`。

---

## 2. 架构与数据流

```
Zabbix 前端（dashboard / URL widget，iframe）
   │  同源路径 /ai/
   ▼
nginx（与 Zabbix 前端同一个 server 块、同一端口）
   │  location ^~ /ai/  →  proxy_pass http://127.0.0.1:10801/
   ▼
FastAPI 应用（仅监听 127.0.0.1:10801）
   ├─ 鉴权：解析并校验 zbx_session cookie → 未登录/失效 401，Zabbix 不可达 503
   ├─ 普通提问：LLM 决定调用工具 → 工具查 Zabbix → LLM 汇总回答
   ├─ 斜杠命令：由代码确定解析并发送，不经 LLM 决策（保证"报告成功=确实成功"）
   └─ MySQL：仅存插件自己的两张表
        ├─ sessions   会话历史（按登录会话 sessionid 隔离）
        └─ audit_log  审计（谁、何时、向哪个供应商发了什么）
```

两个关键设计（决定了部署方式）：

- **监控数据全部经 Zabbix JSON-RPC API 获取**，携带登录用户会话 → 权限天然按用户隔离；
  **插件不读 Zabbix 的数据库表**。因此 MySQL 只需独立小库，不要复用 zabbix 的库与账号。
- **必须与 Zabbix 前端同源（同域名 + 同端口）**，否则浏览器不发送 `zbx_session` cookie，
  页面会一直提示"会话已失效"。

---

## 3. 环境要求

| 组件 | 要求 | 检查命令 |
|---|---|---|
| 操作系统 | Ubuntu 22.04 / 24.04 | `lsb_release -a` |
| Zabbix | 6.0 LTS 或 7.0（含前端与 API） | `zabbix_server -V` |
| MySQL | 8.0 | `mysql --version` |
| nginx | 任意近期版本 | `nginx -v` |
| Python | 3.10+ **且已装 venv/ensurepip** | `python3 -V`；`python3 -c "import ensurepip"` |
| LLM | 支持 **Function Calling** 的 OpenAI 兼容 API Key | 见 [7.3](#73-llm-后端) |

> ⚠️ **Ubuntu 默认不带 venv 支持**，不装会直接失败（`ensurepip is not available`）。
> 但**生产机上不要直接 `sudo apt install`** —— 详见 [3.1 依赖安装的安全做法](#31-依赖安装的安全做法务必先读)。
> 最省事且最安全的是**完全不用 apt**（方案 A）。

### 3.1 依赖安装的安全做法（务必先读）

> 💡 **动手前先跑一次只读体检**：`bash deploy/preflight.sh`
> （不需要 root，不改动系统）。它会明确告诉你这台机器**是否真的缺 venv 支持** ——
> 很多生产机本来就带了（输出 `[OK] ensurepip 可用`），那就**什么都不用装**，
> 直接跳到[第 5 节](#5-部署步骤)。**不要因为"文档说 Ubuntu 默认不带"就先去装包。**

在**生产机**上裸跑 `sudo apt install` 有实际风险：

- apt 可能为满足依赖**顺带升级 `python3` 及其依赖**，进而连带升级 `libpython3.x`、
  `ca-certificates` 等系统包；
- 安装/升级过程会**重启相关服务**（`needrestart` 在 Ubuntu 上可能自动重启），
  若 nginx / MySQL / PHP-FPM 的配置或依赖因此变化，监控站点会立刻不可用；
- 若 `/` 或 `/boot` **空间不足**，安装会中途失败，`dpkg` 进入中断状态，
  此时**重启机器有起不来的风险**。

**先按下面顺序判断，再决定用哪个方案。**

#### 方案 A（最推荐）：完全不动系统包，用 `uv`

`uv` 是单文件静态二进制，自己管理 Python 与虚拟环境，**不依赖系统 `python3-venv`/`ensurepip`**，
不碰 apt，最适合受限的生产机：

```bash
# 安装 uv（装到当前用户 ~/.local/bin，不需要 root 装系统包）
curl -LsSf https://astral.sh/uv/install.sh | sh

cd /opt/zabbix-ai-plugin
~/.local/bin/uv venv .venv                                  # 自动挑选/下载合适的 Python
~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt
~/.local/bin/uv pip install --python .venv/bin/python pytest   # 如需在目标机跑测试
```

systemd 的 `ExecStart` 仍指向 `/opt/zabbix-ai-plugin/.venv/bin/uvicorn`，无需改动。

> 若生产机**完全离线**：用 [5.0](#50-获取部署文件离线包推荐) 的离线包，
> 在联网机器上用 `uv pip download -r requirements.txt -d wheels/` 备好 wheel 一并带入。

#### 方案 B：必须用 apt 时，先看清影响面

```bash
# 1) 先干跑：只看会装/会升什么，不改动系统
sudo apt update
sudo apt-get install -s python3-venv python3-pip | grep -E "^(Inst|Conf|Remv)"

# 2) 若输出里出现你不希望被动到的包（zabbix* / mysql* / nginx / php* / libc6 等）→ 停止，改用方案 A

# 3) 影响面可接受时，先"钉住"关键包，再最小化安装
sudo apt-mark hold zabbix-server-mysql zabbix-frontend-php zabbix-agent \
                    mysql-server nginx
sudo apt-get install -y --no-install-recommends python3-venv
# Ubuntu 24.04 若报找不到 python3-venv，用 python3.12-venv
# 注意：python3-pip 对本项目非必需（venv 内的 pip 已足够），可不装
```

**安装后立即确认关键服务与站点仍正常：**

```bash
systemctl --failed
systemctl is-active zabbix-server mysql nginx
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:801/   # Zabbix 前端（端口按实际，本参考环境是 801）
```

> 🔴 **如果这步出现问题，先不要重启机器**，按 [附录 B：apt 操作后生产异常的恢复](#附录-bapt-操作后生产异常的恢复) 处理。

网络与端口要求：

- 应用服务器需能访问：LLM 接口（如 `open.bigmodel.cn`）、钉钉 `oapi.dingtalk.com`、本机 Zabbix API
- **10801 不需要对外开放**（只监听 127.0.0.1，由 nginx 反代）；请勿在安全组放行 10801
- 只需保证 Zabbix 前端端口（如 80/443/801）对使用者可达

---

## 4. 部署前检查

```bash
cd zabbix-dashboard-plugin
MYSQL_PWD='你的MySQL_root密码' bash deploy/preflight.sh
```

脚本会检查：OS、Python/venv、MySQL 连通与建库情况、Zabbix 服务与版本、nginx 语法、
**监听端口（用于确认哪个是 Zabbix 前端）**、插件端口是否被占用、内存磁盘。
存在阻断性问题时以退出码 1 结束。

> 若有 FAIL 项，请先处理完再继续。

---

## 5. 部署步骤

以下命令中的 `你的zabbix地址`、端口、密码请替换为实际值。

### 5.0 获取部署文件（离线包，推荐）

**① 在开发机生成离线包**（仅用 Python 标准库，Windows / Linux 均可）：

```bash
python deploy/package.py                 # 产物在 dist/
python deploy/package.py --out /tmp/dist # 换输出目录（默认 dist/）
python deploy/package.py --with-tests    # 额外打入测试文件，便于目标机自测
```

> 打包前会做**内容级密钥检查**：命中 LLM Key 形态、`sk-` 开头、钉钉
> `access_token=<32 位以上>`、私钥内容，或 `.env.example` 里的 `LLM_API_KEY` 不是占位符，
> 都会**直接中止**并指出文件（`.env`、`__pycache__` 等则由白名单机制排除）。
>
> 包内的 `.env.example` 由打包脚本**现场生成**（常量内容、所有值为空），**不再复制仓库文件**——
> 即使仓库里的模板将来被误填真实值，也不会随包分发（第三方审计 C-1 的结构性修复）。
> 版本号取自 `git describe --tags`，所以产物名通常是 `zabbix-ai-plugin-v1.0.9-3-g549b546.tar.gz`
> 这种形式——`-3-g` 表示领先 v1.0.9 三个提交，便于核对是不是最新代码。

产出两个文件：

```
dist/zabbix-ai-plugin-<版本>.tar.gz           # 约 100 KB，26 个文件
dist/zabbix-ai-plugin-<版本>.tar.gz.sha256    # 校验值
#  <版本> 形如 v1.0.9 或 v1.0.9-3-g549b546（领先 tag 三个提交时带 -N-g<短哈希>）
```

包内布局（解压后顶层目录独立，不会污染当前目录）：

```
zabbix-ai-plugin-<版本>/
├─ MANIFEST.txt          # 版本、commit、构建时间、每个文件的 SHA256
├─ app/                  # 9 个后端文件（含 __init__.py）
├─ frontend/
│  ├─ index.html
│  └─ launcher.js        # 漂浮聊天球（可选功能，见 5.10）
├─ deploy/               # schema.sql / nginx 示例 / systemd / preflight / verify / package / install_launcher
├─ config.yaml           # 模板（需按生产改）
├─ .env.example          # **打包时现场生成**：所有值为空，需生成 .env 并填写
├─ requirements.txt
├─ README.md / README.en.md
├─ DEPLOYMENT.md
├─ RECOVERY-IDC.md
└─ LICENSE               # GNU GPL v3.0 全文
```

> 🔒 **包内不含任何密钥**：打包脚本用**白名单**收集文件，`.env`、`.venv`、
> `__pycache__`、`.git` 在机制上就不可能被打进去；打包前还会做**内容级检查**——
> 命中 LLM Key 形态、`sk-` 开头、钉钉 `access_token=<32 位以上>`、私钥内容，
> 或 `.env.example` 的 `LLM_API_KEY` 不是占位符，都会直接中止并指出文件。
> `config.yaml` 只是模板；真正的密钥（LLM Key、数据库密码、钉钉 token）
> 需在目标机上由 `.env.example` 生成 `.env` 后填写。

**② 上传并在目标机解压：**

```bash
# 上传（连同校验文件）
scp dist/zabbix-ai-plugin-*.tar.gz* user@生产机:/tmp/

# 校验完整性（建议必做）
cd /tmp
PKG=$(ls zabbix-ai-plugin-*.tar.gz | head -1)   # 实际文件名带 -N-g<短哈希> 后缀
echo "$PKG"
sha256sum -c "$PKG.sha256"
# 期望：<包名>: OK

# 解压到部署目录
sudo mkdir -p /opt/zabbix-ai-plugin
sudo tar -xzf "$PKG" -C /opt/zabbix-ai-plugin --strip-components=1

# 核对内容（应看到 MANIFEST.txt）
ls /opt/zabbix-ai-plugin
head -12 /opt/zabbix-ai-plugin/MANIFEST.txt
```

> `--strip-components=1` 去掉顶层的 `zabbix-ai-plugin-<版本>/`，把内容直接铺进
> `/opt/zabbix-ai-plugin`；不加则会在部署目录下多套一层子目录。

> 💡 **解压后首次启动，`/api/health` 里 `mysql` 会是 `false`，这是正常的** ——
> 包内 `config.yaml` 只是模板，且此时还没有 `.env`（数据库密码为空）。
> 按 5.4 生成 `.env` 并填入 `MYSQL_PASSWORD` 后即为 `true`。
> 实测：包解压后不配置任何东西也能正常启动并响应（页面 200、未登录 401）。

完成后继续 [5.1](#51-建数据库)。

> ⚠️ **升级时**：包内**不含** `config.yaml` 的生产值与本机 `.env`，正常解压不会覆盖它们；
> 但若你手工 `cp -r` 覆盖整个目录就会丢。覆盖前先备份：
>
> ```bash
> sudo cp /opt/zabbix-ai-plugin/config.yaml /opt/zabbix-ai-plugin/config.yaml.bak
> sudo cp /opt/zabbix-ai-plugin/.env       /opt/zabbix-ai-plugin/.env.bak
> ```

**替代方式：git 拉取**

```bash
git clone <仓库地址> /opt/zabbix-ai-plugin
cd /opt/zabbix-ai-plugin && git checkout main    # 用默认分支；勿 checkout 历史标签（见 1. 的提醒）
```
多出的 `tests/` 等仅约 50 KB，无害，且以后 `git pull` 升级更方便。

---

### 5.1 建数据库

> ⚠️ **先确认 MySQL 的监听地址**，它决定账号该建在哪个 host、以及 `config.yaml` 里填什么：
>
> ```bash
> ss -tlnp | grep 3306
> sudo grep -rn "bind-address" /etc/mysql/
> ```
>
> - 若监听 `127.0.0.1:3306` → 账号建 `'zabbix_ai'@'127.0.0.1'`，配置填 `host: "127.0.0.1"`
> - 若**只监听某个具体 IP**（例如 `bind-address = 10.x.x.x`，本参考生产环境就是这种）
>   → 账号必须建 `'zabbix_ai'@'10.x.x.x'`，配置也要填 `host: "10.x.x.x"`。
>   否则会出现 `Connection refused`（连 127.0.0.1 无监听）或 `Access denied`
>   （账号 host 与连接来源地址不匹配）。
>   **不要为此改动 MySQL 的 `bind-address`**（生产上要重启数据库，风险高）；
>   跟随该机器既有做法即可 —— Zabbix 自身的 `DBHost` 通常就是这个 IP。

```bash
# 1) 先把脚本里的 'strong-password' 改成你自己的强密码
#    若 MySQL 只监听特定 IP，同时把脚本里账号的 host 改成该 IP
vi deploy/schema.sql

# 2) 执行（依次建库 → 建账号授权 → 建表）
sudo mysql < deploy/schema.sql

# 3) 验证
sudo mysql -e "SHOW TABLES;" zabbix_ai_plugin
sudo mysql -e "SHOW GRANTS FOR 'zabbix_ai'@'127.0.0.1';"   # host 按 5.1 实际值替换（远程库不是 127.0.0.1）

# 4) 用插件账号实测一次（host/密码换成实际值）
mysql -h <MySQL监听地址> -u zabbix_ai -p -e "SELECT DATABASE()" zabbix_ai_plugin
```

**两种权限模式**（按 DBA 要求选）：

| 模式 | `config.yaml` | 账号权限 | 谁建表 |
|---|---|---|---|
| **A（推荐，默认）** | `auto_init: true` | `SELECT INSERT UPDATE DELETE CREATE INDEX` | 应用启动时自动建表 |
| **B（DBA 管控）** | `auto_init: false` | 仅 `SELECT INSERT UPDATE DELETE` | 由 `schema.sql` 预建 |

> **为什么不用 Zabbix 的库和账号？** 插件不读 Zabbix 的表；复用会让这个 Web 应用拿到
> 整个 zabbix 库的读写权，并把插件表混入 Zabbix schema，升级/还原时互相牵连。
> 独立账号只需三行 SQL。若数据库不在本机，请把 `schema.sql` 中 `'zabbix_ai'@'127.0.0.1'`
> 的 host 改成应用服务器 IP，并同步 `config.yaml` 的 `storage.mysql.host`。

### 5.2 放置代码

```bash
sudo mkdir -p /opt/zabbix-ai-plugin
sudo chown -R zabbix:zabbix /opt/zabbix-ai-plugin     # 运行用户，可按需改为专用账号
# 将项目文件放入 /opt/zabbix-ai-plugin（scp / rsync / git clone 均可）
```

### 5.3 安装依赖

```bash
cd /opt/zabbix-ai-plugin
sudo -u zabbix python3 -m venv .venv
sudo -u zabbix .venv/bin/pip install --upgrade pip
sudo -u zabbix .venv/bin/pip install -r requirements.txt
# 国内加速：加 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 5.4 写 `.env`（所有密钥）

```bash
sudo -u zabbix cp .env.example .env
sudo -u zabbix vi .env
sudo chmod 600 .env && sudo chown zabbix:zabbix .env
```

必填项：

```bash
LLM_API_KEY=你的LLM密钥
MYSQL_PASSWORD=与schema.sql一致的密码
```

按需项（供应商钉钉）：

```bash
DINGTALK_SUPPLIER_A=你的钉钉机器人access_token
```

### 5.5 改 `config.yaml`

最少改这三处：

```yaml
zabbix:
  api_url: "http://你的zabbix地址:801/api_jsonrpc.php"   # ← 必改（服务端可走 127.0.0.1）

suppliers:                                               # ← 按实际供应商改
  SUPPLIER_A:
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_SUPPLIER_A}"

history:                                                 # ← 可选：不写则用下面的默认值
  ttl_minutes: 15        # 闲置多久算新会话（注销重登本来就是新会话）
  max_messages: 50       # 送入模型的历史条数上限
  retention_days: 7      # 兜底清理：启动时删除超过这么久的历史记录

storage:
  mysql:
    password_env: "MYSQL_PASSWORD"
```

其余字段见 [第 7 节](#7-配置参考)。

### 5.6 启动并自检（先手工跑通，再上 systemd）

```bash
cd /opt/zabbix-ai-plugin
sudo -u zabbix .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 10801
```

另开终端：

```bash
curl -s http://127.0.0.1:10801/api/health
# 期望：{"ok":true,"mysql":true,"llm_model":"...","zabbix_api_configured":true}
```

`mysql:false` 说明数据库配置不对，先解决再继续。确认后 `Ctrl+C`。

### 5.7 systemd 常驻

```bash
sudo cp deploy/zabbix-ai-plugin.service /etc/systemd/system/
# 如运行用户/路径不同，编辑 User=、WorkingDirectory=、EnvironmentFile=、ExecStart=
sudo systemctl daemon-reload
sudo systemctl enable --now zabbix-ai-plugin
systemctl status zabbix-ai-plugin --no-pager
```

### 5.8 nginx 反代

**`deploy/nginx.conf.example` 是片段，不能直接当配置文件用**，请把其中的 `location`
**合并进你现有的 Zabbix server 块**（不要新建 server 块，会与 Zabbix 站点冲突）：

```nginx
    # AI 对话框插件 -> FastAPI (10801)
    location ^~ /ai/ {
        proxy_pass              http://127.0.0.1:10801/;
        proxy_http_version      1.1;
        proxy_set_header        Host              $host;
        proxy_set_header        X-Real-IP         $remote_addr;
        proxy_set_header        X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header        X-Forwarded-Proto $scheme;
        proxy_buffering         off;
        proxy_read_timeout      300s;
    }
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

**两个必须注意的点：**

1. ⚠️ **必须写 `location ^~ /ai/`，`^~` 不能漏。**
   Zabbix 自带配置含 `location ~ /(api\/|conf[^\.]|include|locale) { deny all; return 404; }`，
   本插件接口形如 `/ai/api/health` 含 `api/`，会被该正则拦截而**全部 404**。
   `^~` 命中前缀后跳过正则检查。
2. 📌 **必须加在「提供 Zabbix 前端」的那个 server 块里**（同端口才能同源）。
   判断方法：`ss -tlnp | grep nginx` 看哪个端口是 Zabbix 前端；
   注意 80 端口可能只是 nginx 默认站点而非 Zabbix。

### 5.9 在 dashboard 中接入

1. **Dashboards** → 打开目标 dashboard → **Edit dashboard**
2. **Add** → 选择 **URL** 类型 widget
3. **Name** 填 `AI 助手`；**URL** 填 `http://你的zabbix地址:801/ai/`
   （**端口必须与 Zabbix 前端一致**）
4. **Add** → **Save**，按需调整大小

或用 API 直接创建（把 `<API_TOKEN>` 换成 Zabbix API Token）：

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"dashboard.create","params":{
        "name":"AI 助手","display_period":30,"auto_start":1,
        "pages":[{"name":"AI 助手","widgets":[{
          "type":"url","name":"AI 助手","x":0,"y":0,"width":24,"height":8,
          "fields":[{"type":1,"name":"url","value":"http://你的zabbix地址:801/ai/"}]}]}]},
       "auth":"<API_TOKEN>","id":1}' \
  http://你的zabbix地址:801/api_jsonrpc.php
```

### 5.10（可选）漂浮聊天球：右下角一个球，点开就聊

不想让对话框占 dashboard 网格时，可以改成**漂浮球**：dashboard 右下角一个可拖动的圆球，
点击弹出小面板内嵌对话，不跳转、不刷新（位置会记住）。小球与 URL widget 可以并存。

```bash
# 在 Zabbix 前端所在机器上以 root 执行
sudo python3 /opt/zabbix-ai-plugin/deploy/install_launcher.py             # 安装（幂等）
sudo python3 /opt/zabbix-ai-plugin/deploy/install_launcher.py --status    # 查看状态
sudo python3 /opt/zabbix-ai-plugin/deploy/install_launcher.py --uninstall # 卸载
```

原理与安全性：

- 脚本只做一件事：在 nginx 的 PHP location（`location ~ [^/]\.php`）里加 3 行 `sub_filter`，
  把 `<script src="/ai/launcher.js" defer></script>` 注入到每个 HTML 页面的 `</body>` 前；
- **不改 Zabbix 自身任何文件**，因此 `apt upgrade zabbix-frontend-php` 不会覆盖它；
- 改动前自动备份 nginx 配置；`nginx -t` 不通过会自动回滚；重复执行不会重复注入；
- `sub_filter_types text/html` 只作用于 HTML 响应，**不影响 Zabbix 的 JSON/AJAX 接口**；
- 浏览器路径带 gzip 也正常：`sub_filter` 在 gzip 之前执行（nginx 1.18 与 1.24 均实测通过）。

显示范围：注入是无条件的，但**只有 dashboard 页面且已登录**时 `launcher.js` 才会创建小球
（其余页面直接跳过，包括登录页与会话失效页）。权限与 URL widget 一致——iframe 同源加载 `/ai/`，
自动复用 `zbx_session`，每个人仍是自己权限内的数据。

回滚（两种都行）：

```bash
sudo python3 /opt/zabbix-ai-plugin/deploy/install_launcher.py --uninstall
# 或直接还原备份
sudo cp -a /etc/nginx/conf.d/zabbix.conf.bak.launcher.<时间戳> /etc/nginx/conf.d/zabbix.conf
sudo systemctl reload nginx
```

排查：

```bash
# 页面上应能看到注入的 script 标签
curl -s http://127.0.0.1:801/zabbix.php?action=dashboard.view | grep -m1 launcher.js
# 脚本本身应可访问（由插件提供，注意只有 GET）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:801/ai/launcher.js
```

---

## 6. Zabbix 侧必做设置

### 6.1 关闭 iframe 沙箱对脚本的限制（**不做则插件"能打开但完全不能用"**）

Zabbix 6.0/7.0 在 **Administration → General → Other** 有 **Use iframe sandboxing**
（**默认开启**）。开启时 URL widget 的 iframe 会带上 `sandbox` 属性，而该属性的值
**直接取自同一页面的 `Iframe sandboxing exceptions` 字段**（该字段命名有误导性，
它实际是 sandbox 属性值，不是 URL 白名单）。

默认该字段为空 → iframe 变成 `sandbox=""` → **脚本、同源、Cookie 全部被禁**。症状：

- 对话框能正常显示（看起来部署成功了）
- 输入 `/` **不弹出供应商**
- 点「发送」**毫无反应**（页面 JS 根本没执行）
- nginx 日志里**完全没有** `/ai/api/*` 请求

**处理（二选一）：**

1. **推荐**：把 **Iframe sandboxing exceptions** 填为下面这串，保存后刷新 dashboard：

   ```
   allow-scripts allow-same-origin
   ```

2. 或取消勾选 **Use iframe sandboxing**（对所有 URL widget 生效，安全性更低）。

命令行等价操作：

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"settings.update","params":{"iframe_sandboxing_exceptions":"allow-scripts allow-same-origin"},"auth":"<API_TOKEN>","id":1}' \
  http://你的zabbix地址:801/api_jsonrpc.php
```

> ⚠️ 该设置**对所有 URL widget 全局生效**。若还有其它 URL widget 承载不可信外部内容，
> 请评估风险；替代方案是**不用 dashboard widget**，直接把 `/ai/` 作为独立页面打开
> （加入浏览器书签，或配置 Zabbix 自定义菜单项）。独立页面不受 iframe sandbox 影响，
> 功能完全一致，且天然支持主题跟随系统偏好。

### 6.2 建议：为插件创建最低权限的 Zabbix 用户（可选）

插件透传登录用户会话，数据可见范围完全由该用户的 Zabbix 权限决定。
若希望使用插件的运维人员只能看到部分主机，为其分配对应权限的 Zabbix 用户即可，
插件无需感知。

---

## 7. 配置参考

### 7.1 `config.yaml`

| 字段 | 说明 |
|---|---|
| `zabbix.frontend_url` | 仅展示用，可留默认 |
| `zabbix.api_url` | **必改**。JSON-RPC 地址；服务端可与前端同机走 `http://127.0.0.1:801/api_jsonrpc.php` |
| `zabbix.auth.mode` | 保持 `user`：透传登录用户会话，权限按用户隔离 |
| `llm.base_url` / `llm.model` / `llm.api_key_env` | LLM 后端，见 7.3 |
| `llm.temperature` / `llm.top_p` | 默认 `0.3` / `0.9`；工具调用需确定性，不建议调高 |
| `llm.max_tokens` | 单次回复上限，默认 1024 |
| `llm.function_calling` | **必须 `true`**，否则退化为提示词解析，稳定性下降 |
| `llm.extra_params` | 透传后端特有参数，默认空 |
| `suppliers` | 供应商白名单，键名即 `/命令` 的名字，可任意多个，见 7.4 |
| `server.host` | 保持 `127.0.0.1`（由 nginx 反代） |
| `server.port` | 默认 `10801`，需与 systemd 与 nginx 三处一致 |
| `server.base_path` | 默认 `/ai` |
| `storage.mysql.*` | 数据库连接；`password_env` 填环境变量名 |
| `storage.mysql.auto_init` | 见 5.1 的两种模式 |

### 7.2 `.env`

| 变量 | 用途 |
|---|---|
| `LLM_API_KEY` | LLM 密钥（**必填**） |
| `MYSQL_PASSWORD` | 数据库密码（**必填**） |
| `DINGTALK_<NAME>` | 各供应商群机器人 access_token |
| `DINGTALK_<NAME>_SECRET` | 加签密钥（仅加签模式） |
| `ZABBIX_SERVICE_*` | 仅 `auth.mode=service` 时使用，默认不需要 |

> 应用启动时会自动加载同目录 `.env`（也兼容 systemd 的 `EnvironmentFile`），
> 因此手工 `uvicorn` 启动同样能读到密钥。

### 7.3 LLM 后端

当前默认：

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

切换厂商只改 `base_url` 与 `model`，代码无需改动：

| 厂商 | `base_url` | `model` 示例 |
|---|---|---|
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |

**上线前先验证模型可用且支持 Function Calling：**

```bash
cd /opt/zabbix-ai-plugin
sudo -u zabbix .venv/bin/python - <<'PY'
import os, httpx
key = os.environ["LLM_API_KEY"]
url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
tools = [{"type":"function","function":{"name":"query_problems","description":"查告警",
          "parameters":{"type":"object","properties":{"days":{"type":"integer"}},"required":["days"]}}}]
print("plain:", httpx.post(url, headers={"Authorization": f"Bearer {key}"},
      json={"model":"glm-4-flash","messages":[{"role":"user","content":"你好"}]}, timeout=60).status_code)
r = httpx.post(url, headers={"Authorization": f"Bearer {key}"},
      json={"model":"glm-4-flash","tool_choice":"auto","tools":tools,
            "messages":[{"role":"user","content":"查一下最近3天的告警"}]}, timeout=60)
print("tools:", r.status_code, "tool_calls" if "tool_calls" in r.text else "NO tool_calls")
PY
```

第二条必须出现 `tool_calls`，否则该模型不适合本插件。

> ⚠️ **额度按"轮次"折算**：工具调用是多轮请求，一次提问通常消耗 2~4 次请求
> （决策 1 次 + 每轮工具 1 次 + 汇总 1 次）。团队多人使用请据此评估额度。

### 7.4 供应商（钉钉）

`config.yaml` 的 `suppliers` 键名就是命令里的名字：

```yaml
suppliers:
  SUPPLIER_A:
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_SUPPLIER_A}"
  机房供应商B:
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_IDC_B}"
    keyword: "告警"                            # 关键词模式
    # secret_env: "DINGTALK_IDC_B_SECRET"      # 加签模式
```

| 机器人后台安全设置 | `config.yaml` 要填 |
|---|---|
| **自定义关键词** | `keyword: "告警"`，与后台**完全一致** |
| **加签** | `secret_env: "DINGTALK_xxx_SECRET"`，密钥填进 `.env` |
| **IP 白名单** | 不用填，把服务器出口公网 IP 加白 |
| **无安全设置** | 两个都不填，只给 `webhook`（已实测可用） |

使用方式：`/SUPPLIER_A 出口丢包严重，请协助排查`。名字可唯一前缀简写（`/SUP`）；
前缀命中多个会被拒绝并列出可选，未注册名称一律拒绝。

> 兼容简写：`供应商名: "https://...webhook..."` 直接给 URL 字符串也可以。

#### 消息正文格式（可自定义）

发到群里的正文默认是 `[zabbix-AI] {username} 上报：\n{message}`。
想改前缀、或去掉"某某 上报"这段，**不用改代码**，改 `config.yaml` 即可：

```yaml
# 全局默认模板；占位符：{username} 发送人（Zabbix 登录名）、{message} 消息正文
dingtalk:
  message_template: "[zabbix-AI] {username} 上报：\n{message}"
```

常用改法：

| 想要的效果 | 写法 |
|---|---|
| 保留默认 | `"[zabbix-AI] {username} 上报：\n{message}"` |
| **去掉发送人**（只发正文） | `"{message}"` |
| 换前缀、保留发送人 | `"【监控告警】{username} 反馈：\n{message}"` |
| 前缀在前、正文换行 | `"【Zabbix】\n{message}"` |

也可以在**单个供应商**下覆盖（优先于全局）：

```yaml
suppliers:
  网络供应商A:
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_NETWORK_A}"
    message_template: "【来自监控】{message}"
```

> - 优先级：供应商级 `message_template` > 全局 `dingtalk.message_template` > 内置默认
> - 模板里未知的占位符原样保留；正文中的 `{}` 不会被破坏
> - 改完 `config.yaml` 需 `systemctl restart zabbix-ai-plugin` 生效
> - 若机器人用了"自定义关键词"模式，模板里**必须仍包含该关键词**，否则会被钉钉拒收（errcode 310000）


---

## 8. 部署后自检

```bash
cd zabbix-dashboard-plugin
MYSQL_PWD='MySQL_root密码' ZBX_USER=Admin ZBX_PASS='Zabbix管理员密码' \
  BASE_URL=http://127.0.0.1:801 bash deploy/verify.sh
```

> **`BASE_URL` 必须带 Zabbix 前端端口**（本参考环境为 `:801`）。
> 若填错，脚本会自动探测常见端口并给出可直接复制的正确命令。

脚本检查 11 项，全通过时输出 `PASS=11 FAIL=0`，其中包含两个关键项：

- **iframe sandbox 设置**：未放行脚本会明确 FAIL 并给出处理位置
- **真实浏览器同款鉴权**：用 URL 编码的 `zbx_session` cookie 请求 `/ai/api/history`，
  期望 200（这是 dashboard 内真实路径；未编码形式也应 200）

---

## 9. 浏览器验收

在浏览器（已登录 Zabbix）逐个确认：

- [ ] dashboard 内对话框**背景为亮色**，与 Zabbix 主题一致；右上角有「**主题**」按钮
- [ ] 输入框打一个 `/` → **下方弹出供应商名可点选**
- [ ] 输入 `最近有哪些告警？` → 点「发送」→ **有回复**
- [ ] 输入 `现在监控了哪些主机？` → 回复与 Zabbix 实际主机一致
- [ ] `/SUPPLIER_A 测试消息` → **钉钉群收到**，且对话框显示发送成功与时间
- [ ] 退出 Zabbix 登录后访问 `/ai/` → 提示会话失效（不泄露数据）
- [ ] 直接访问 `http://你的zabbix地址:801/ai/api/history`（未登录）→ **401**

> 若第 1~3 项失败，优先检查 [6.1](#61-关闭-iframe-沙箱对脚本的限制不做则插件能打开但完全不能用)。
> 若显示「会话已过期」而 Zabbix 已登录，检查 `/ai/` 是否与 Zabbix **同源**（同端口）。

---

## 10. 生产加固清单

**必须做：**

- [ ] **改掉 Zabbix 默认管理员口令**（`Admin/zabbix`）；若 Zabbix 前端对公网开放尤其重要
- [ ] `zabbix_ai` 数据库账号使用**独立强密码**，不要与 root 或 Zabbix 账号共用
- [ ] `.env` 权限 `600`、属主为运行用户；确认 `.env` 不在版本库中
- [ ] **不要对公网放行 10801**（应用只监听 127.0.0.1）
- [ ] 数据库账号遵循最小权限（模式 B 时不给 DDL）
- [ ] LLM API Key 与钉钉 token 定期轮换，勿写入 `config.yaml`

**建议做：**

- [ ] Zabbix 前端启用 HTTPS（若经公网访问）；本插件随 Zabbix 同源，无需单独证书
- [ ] 在 nginx 对 `/ai/` 增加访问控制或限速（如 `limit_req`），防止 LLM 额度被刷
- [ ] 审计日志定期归档（见 11.4）
- [ ] 为插件单独建运行用户（而非复用 `zabbix`），进一步隔离
- [ ] 监控插件自身：对 `/api/health` 做 Zabbix 监控项（HTTP agent），
      `mysql:false` 或接口不可用时告警——可以直接用本插件监控它自己
- [ ] 定期 `BASE_URL=http://127.0.0.1:801 bash deploy/verify.sh` 并纳入巡检
      （**不带 `BASE_URL` 会按 80 端口探测**，参考环境下必然失败）

---

## 11. 运维：日志 / 升级 / 回滚 / 数据保留

### 11.1 日志

```bash
systemctl status zabbix-ai-plugin
journalctl -u zabbix-ai-plugin -n 100 -f
# 关键日志：
#   zabbix_ai.tools  tool call / tool result（每次工具调用及结果摘要）
#   zabbix_ai.auth   鉴权失败原因（cookie 缺失 / 无法解析 / zabbix 判定无效）
```

### 11.2 重启与配置变更

```bash
sudo systemctl restart zabbix-ai-plugin     # 改 config.yaml / .env 后需重启
```

### 11.3 升级

```bash
cd /opt/zabbix-ai-plugin
sudo systemctl stop zabbix-ai-plugin
sudo cp -a /opt/zabbix-ai-plugin /opt/zabbix-ai-plugin.bak.$(date +%F)   # 先备份
# 更新代码（覆盖 app/ frontend/ deploy/ 等，保留你的 config.yaml 与 .env）
sudo -u zabbix .venv/bin/pip install -r requirements.txt
sudo systemctl start zabbix-ai-plugin
BASE_URL=http://127.0.0.1:801 bash deploy/verify.sh   # 再验一遍（BASE_URL 必须带 Zabbix 前端端口）
```

前端已设置 `Cache-Control: no-store`，浏览器**普通刷新**即可拿到新版本，无需强刷。

### 11.4 回滚

```bash
sudo systemctl stop zabbix-ai-plugin
sudo rsync -a --delete /opt/zabbix-ai-plugin.bak.<日期>/ /opt/zabbix-ai-plugin/
sudo systemctl start zabbix-ai-plugin
```

数据库表结构向后兼容（`CREATE TABLE IF NOT EXISTS`），回滚无需改库。

### 11.5 数据保留（**生产必做，否则会无限增长**）

`sessions`（会话历史）与 `audit_log`（审计）会持续增长。建议按合规要求定期清理，
例如保留会话 90 天、审计 365 天：

```sql
DELETE FROM sessions  WHERE ts < UNIX_TIMESTAMP(NOW() - INTERVAL 90 DAY);
DELETE FROM audit_log WHERE ts < UNIX_TIMESTAMP(NOW() - INTERVAL 365 DAY);
```

加入 cron（每周一次）：

```bash
sudo tee /etc/cron.weekly/zbxai-purge >/dev/null <<'EOF'
#!/bin/sh
mysql zabbix_ai_plugin -e "DELETE FROM sessions WHERE ts < UNIX_TIMESTAMP(NOW() - INTERVAL 90 DAY); DELETE FROM audit_log WHERE ts < UNIX_TIMESTAMP(NOW() - INTERVAL 365 DAY);"
EOF
sudo chmod +x /etc/cron.weekly/zbxai-purge
```

> 审计日志含"谁向哪个供应商发了什么"，保留期限请与你们的审计要求对齐。

### 11.6 备份

需要备份的只有三样：`config.yaml`、`.env`、以及 MySQL 库 `zabbix_ai_plugin`。

```bash
mysqldump --single-transaction zabbix_ai_plugin > zbxai-$(date +%F).sql
```

---

## 12. 故障排查

| 现象 | 原因与处理 |
|---|---|
| **对话框能显示，但打 `/` 无提示、发送无反应** | Zabbix iframe sandbox 禁用了脚本 → [6.1](#61-关闭-iframe-沙箱对脚本的限制不做则插件能打开但完全不能用)。自检：nginx 日志里完全没有 `/ai/api/*` 即为此问题 |
| 提示"会话已过期"，重新登录也无效 | `/ai/` 与 Zabbix 前端不同源（端口/域名不一致）→ 确认加在 Zabbix 前端那个 server 块；另见版本 v1.0.0 已修复的 cookie URL 解码问题（附录 A） |
| `/ai/` 打开是 nginx 404 | ① `location` 是否写成 `^~ /ai/`；② `proxy_pass` 结尾 `/` 不能少；③ 是否 reload nginx |
| `/api/health` 返回 `mysql:false` | ① `.env` 的 `MYSQL_PASSWORD` 与建库时不一致；② `storage.mysql.user/host` 不对；③ **MySQL 只监听特定 IP**（`bind-address` 非 127.0.0.1）却仍填 `host: "127.0.0.1"` → `Connection refused`；④ 账号 host 与连接来源地址不匹配 → `Access denied`。③④ 见 [5.1](#51-建数据库) |
| 提问报「（服务异常）」 | LLM Key 无效/额度不足；按 7.3 的脚本先验证 LLM 连通性与 tool_calls |
| 回答总是「达到工具调用上限」 | 模型 Function Calling 能力弱，换更强模型 |
| `/供应商名` 提示"未注册供应商" | 名称不在 `config.yaml` 的 `suppliers` 下（需完全一致，或用唯一前缀） |
| `/供应商名` 发送失败并显示 errcode | 按 errcode 处理：`40035` token 为空；`310000` 关键词不匹配/加签错误/IP 不在白名单；`300001` token 无效 |
| 配色与 Zabbix 不一致 | 点「主题」切到「跟随」；或确认已部署新版前端；也可用 `?theme=light` 强制 |
| 端口冲突 | 改 `config.yaml` 的 `server.port`，**同步** systemd 的 `--port` 与 nginx 的 `proxy_pass`（三处一致） |
| 服务起不来 | `journalctl -u zabbix-ai-plugin -n 50`；常见为 `.env` 缺失或权限不对 |

---

## 附录 A：历史踩坑与版本变更

**每个版本的「用户可见现象 → 根因 → 现在怎样」已整理到 [`CHANGELOG.md`](CHANGELOG.md)**，
并标注了对应提交与代码位置。部署前建议扫一眼最新两三版，本节不再复述那 20 条。

两条**跨版本仍然适用**的工程教训，部署时请记住：

1. **部署路径不能依赖运维手工步骤。** `sessions` 表曾因"改列需要 `ALTER` 权限"而迁移失败，
   而运行期账号按 `schema.sql` 只有 `SELECT/INSERT/UPDATE/DELETE/CREATE/INDEX`（实测
   `1142 ALTER command denied`），失败还只记 warning，表现为**每次对话 502**。
   现在改为复用既有列（零 DDL、零权限要求），并在启动时做只读结构自检 `check_schema()`——
   存储不可用会在启动日志里留下 **ERROR**，不必等用户提问才发现。
2. **有外部副作用的动作不交给大模型。** 钉钉发送只能由 `/供应商名 消息内容` 触发（确定性路径），
   模型**没有**发送工具：既避免"没发却说发了"，也避免把与监控无关的内容发进供应商群。
   同理，任何"报告成功"都必须等于"确实成功"。

> 回归测试：`python -m pytest`。**不在文档里写死用例数**——每发布一个离线包都会新增参数化用例
> （每个包 3 个），写死必然过期；干净 clone 与带 `dist/` 的开发机收集数也不同。
> 改动约定见 `AGENTS.md`：每次改动必须提交 commit，并在交付前确保测试全部通过。

---

## 附录 B：apt 操作后生产异常的恢复

> 🏢 **如果机器在 IDC / 是物理机，且 SSH 已经连不上**：请直接用
> [`RECOVERY-IDC.md`](RECOVERY-IDC.md) —— 那是可打印带走的**现场恢复清单**
> （出发前准备、到场先看什么、系统起不来时的 U 盘 Live 救援、恢复后验证）。
> 本附录适用于**还能登录**（SSH 或本地终端）的情况。

如果你在生产机上执行过 `apt install` / `apt upgrade` 之后出现服务不可用，按下面顺序处理。

### B.0 先做与先别做

**立即停止：**

- ❌ **不要再执行任何 `apt` 命令**（尤其 `apt upgrade`、`apt autoremove`、`apt dist-upgrade`），
  会把状态越弄越乱
- ❌ **不要重启机器**。若 `/boot` 空间不足或 initramfs 生成失败，重启可能直接起不来；
  只要系统还在跑，就有机会就地修好
- ❌ 不要删除 `/var/lib/dpkg` 下的文件

**先做：** 取证 + 判断属于哪一类（下面的 B.1），再动手。

### B.1 判断属于哪一类

```bash
# 1) 磁盘与 inode：apt 崩溃最常见的原因是空间不足
df -h; df -i

# 2) 哪些服务挂了
systemctl --failed

# 3) 关键服务状态
systemctl status zabbix-server zabbix-agent mysql nginx 'php*-fpm' --no-pager -l

# 4) dpkg 是否处于中断状态（有输出就是没配完）
sudo dpkg --audit
dpkg -l | awk '$1 !~ /^ii/ {print}' | head -30

# 5) 这次到底装了什么、升了什么（关键取证）
grep -E "^(Start-Date|Commandline|Install|Upgrade|Remove)" /var/log/apt/history.log | tail -40
zgrep -hE " (install|upgrade) " /var/log/dpkg.log* | tail -50
```

### B.2 按类型处理

#### 类型 1：磁盘/inode 满（`df` 显示 100%）

```bash
# 先腾空间（常见可清理项，确认后再删）
sudo du -xh --max-depth=1 /var | sort -h | tail -10
sudo journalctl --vacuum-size=200M          # 清理 systemd 日志
sudo apt-get clean                          # 清理 apt 缓存
ls -la /var/cache/apt/archives/*.deb 2>/dev/null | head
```

腾出空间后再修 dpkg（见类型 2）。若 **`/boot` 满**：先用 `apt-get -f install` 完成，
再考虑移除旧内核（`dpkg -l 'linux-image-*'`，谨慎操作）。

#### 类型 2：dpkg 中断（`dpkg --audit` 有输出）

```bash
sudo dpkg --configure -a        # 重新配置未完成的包
sudo apt-get -f install         # 修复依赖
sudo dpkg --audit               # 再确认（应无输出）
```

#### 类型 3：服务被顺带重启/升级后起不来

先看具体报错，再对症处理：

```bash
# MySQL / MariaDB
systemctl status mysql --no-pager -l
sudo tail -50 /var/log/mysql/error.log

# nginx（升级后配置不兼容是常见原因）
sudo nginx -t
sudo tail -30 /var/log/nginx/error.log

# PHP-FPM（Zabbix 前端依赖）
systemctl status 'php*-fpm' --no-pager
sudo journalctl -u 'php*-fpm' -n 50 --no-pager

# Zabbix server
systemctl status zabbix-server --no-pager -l
sudo tail -50 /var/log/zabbix/zabbix_server.log
```

按报错修复后逐个拉起：

```bash
sudo systemctl restart mysql          # 先数据库
sudo systemctl restart 'php*-fpm'     # 再 PHP
sudo systemctl restart nginx
sudo systemctl restart zabbix-server zabbix-agent
```

#### 类型 4：机器已经起不来

需要走**云控制台的 VNC / 救援模式**（或物理 iLO/IPMI）进入，常见原因与处理：

- `/boot` 满导致内核/initramfs 未更新：在救援模式下清理旧内核后 `update-initramfs -u -k all`
- 引导项损坏：`grub-install` + `update-grub`
- 若有**快照/备份**，直接回滚是最快且最稳的路径（去云控制台操作）

### B.3 回滚这次升级（谨慎）

```bash
# 1) 从历史日志里找出被 Upgrade 的包
grep "^Upgrade:" /var/log/apt/history.log | tail -5

# 2) 降级单个包（需知道目标版本号）
apt-get install --reinstall <package>=<version>

# 3) 把 apt 标记过 hold 的包先解 hold 再处理
sudo apt-mark showhold
```

> ⚠️ 降级系统包（libc6、python3 等）容易引发新的依赖问题。**有快照就回滚快照**，
> 不建议手工逐个降级。

### B.4 恢复后必须做的

```bash
systemctl --failed                                   # 应无失败单元
systemctl is-active zabbix-server mysql nginx
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:801/ # Zabbix 前端应 200（端口按实际）
# 若已部署本插件
BASE_URL=http://127.0.0.1:801 bash /opt/zabbix-ai-plugin/deploy/verify.sh
```

并确认监控数据没有断档（Latest data 是否有空窗），必要时向相关方说明。

### B.5 下次避免

- 装依赖**优先用 [3.1 方案 A（uv）](#方案-a最推荐完全不动系统包用-uv)**，完全不碰 apt
- 必须用 apt 时：先 `apt-get install -s` 干跑看影响面、`apt-mark hold` 钉住关键包、
  只装最小必需、装完立刻检查服务
- 生产机**禁止** `apt upgrade` / 无人值守自动升级；需要打补丁时先在预生产验证
- 动手前先做快照（云主机打快照通常几分钟）
