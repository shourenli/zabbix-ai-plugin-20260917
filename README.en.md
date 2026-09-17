# Zabbix AI Dialog Plugin (zabbix-ai-plugin)

[简体中文](README.md) ｜ **English**

**License: [GNU General Public License v3.0](LICENSE)** (full text in the "License" section at the end)

> **Current version: v1.1.0** (2026-09-17) ｜ Version history: [`CHANGELOG.md`](CHANGELOG.md) ｜
> Since `v1.1.0` this repository has a **rebuilt, clean history**: it contains no real credentials,
> real hostnames or real IPs
> (cleanup scope and how it was handled: [`SECURITY.md`](SECURITY.md)).

Embeds an AI dialog in a Zabbix dashboard: query alerts and metrics and forecast trends in natural
language, and send a problem to the matching supplier's DingTalk group with one `/supplier-name`
command.

- **Deployment target**: Ubuntu 22.04 / 24.04 + Zabbix 6.0 LTS or 7.0 + MySQL 8.0 + nginx
- **How you reach it**: log in to Zabbix and open it from the dashboard; visiting `/ai/` while logged out does not work (see 4.4)
- **No Zabbix files are modified**: the only optional change is the floating chat ball injected by nginx (see 4.3; revert it with `--uninstall`)

## Documentation Map

| Document | Audience | Contents |
|---|---|---|
| **README.md** (the Simplified Chinese original) | Users / operations | How it works, requirements, quick start, usage, **configuration reference**, troubleshooting, development and testing, directory structure, known limitations |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Production deployers | Deliverables list, offline bundle packaging and deployment, pre-deployment checks, step-by-step deployment, mandatory Zabbix-side settings, hardening checklist, upgrade and rollback, data retention, appendix of pitfalls |
| [`SECURITY.md`](SECURITY.md) | Everyone | Vulnerability reporting channel, trust boundaries, deliberate security design, known limitations, notes on legacy secrets |
| [`RECOVERY-IDC.md`](RECOVERY-IDC.md) | On-site IDC staff | On-site recovery checklist (printable, take it with you) |
| [`CHANGELOG.md`](CHANGELOG.md) | Everyone | v1.0.0 – v1.0.9, every version as "symptom → cause → what happens now" |
| [`AGENTS.md`](AGENTS.md) | Developers | Change conventions (one commit per change, all tests green before delivery) |
| [`LICENSE`](LICENSE) | Everyone | Full text of the GNU General Public License v3.0 |
| [`README.en.md`](README.en.md) | English readers | English version of this document (English translation of the Chinese README) |

**For production deployment, read `DEPLOYMENT.md`**: Section 3 of this document only guarantees that
it runs on a clean machine; the hardening, rollback and data-retention policies required in
production all live over there. Where the two disagree, `DEPLOYMENT.md` wins.

---

## Table of Contents

1. [How It Works](#1-how-it-works)
2. [Requirements](#2-requirements)
3. [Quick Start](#3-quick-start)
4. [Usage](#4-usage)
5. [Configuration Reference](#5-configuration-reference)
6. [Upgrades and Routine Operations](#6-upgrades-and-routine-operations)
7. [Troubleshooting and FAQ](#7-troubleshooting-and-faq)
8. [Development and Testing](#8-development-and-testing)
9. [Directory Structure](#9-directory-structure)
10. [Known Limitations](#10-known-limitations)

---

## 1. How It Works

```
Zabbix frontend (dashboard)
  └─ URL widget (iframe)  ── same-origin path /ai/ ──▶  nginx
                                                   │ reverse proxy
                                                   ▼
                                     FastAPI app (127.0.0.1:10801)
                                       ├─ validate the zbx_session cookie ──▶ 401 if not logged in
                                       ├─ call the LLM (OpenAI-compatible API) → the model decides which tool to call
                                       ├─ five tools (registered for the model):
                                       │    query_problems / query_top / query_metrics
                                       │    query_hosts  / trend_analysis
                                       ├─ /supplier-name command: the DingTalk send is executed
                                       │    deterministically by **code** (the model has **no** send tool, see 4.2)
                                       └─ MySQL: stores only the plugin's own two tables
                                            ├─ sessions   conversation history (isolated per **login session**)
                                            └─ audit_log  audit (who, when, sent to which supplier)
```

Two key points:

- **All monitoring data is fetched through the Zabbix JSON-RPC API**, carrying the logged-in user's
  session (`sessionid` is passed through). Every user therefore sees only the data their permissions
  allow, and the plugin **does not read Zabbix's database tables**.
- **MySQL stores only the plugin's own data**, so it uses a dedicated database plus a dedicated
  least-privilege account and never reuses zabbix's database or account (rationale in 3.1).

---

## 2. Requirements

| Item | Requirement | How to check |
|---|---|---|
| Operating system | Ubuntu 22.04 / 24.04 | `lsb_release -a` |
| Zabbix | 6.0 LTS or 7.0 (including frontend and API) | `zabbix_server -V` |
| MySQL | 8.0 | `mysql --version` |
| nginx | any recent version | `nginx -v` |
| Python | 3.10 or newer, and `python3-venv` is needed | `python3 -V` (see the note below) |
| LLM | an OpenAI-compatible API Key that supports **Function Calling** | see 5.4 |

> **Ubuntu needs venv support installed first**: Ubuntu 22.04/24.04 does not ship `ensurepip` by
> default, so a plain `python3 -m venv` fails with "ensurepip is not available". Run this first:
>
> ```bash
> sudo apt update && sudo apt install -y python3-venv python3-pip
> ```
>
> If `python3-venv` is reported as not found, install the package for your actual version — for
> example on Ubuntu 24.04: `python3.12-venv`.

---

## 3. Quick Start

Replace the domain names, passwords and tokens in the commands below with real values. **In
production, use the offline-bundle flow in `DEPLOYMENT.md` instead (it includes the pre-deployment
check `deploy/preflight.sh` and the post-deployment self-check `deploy/verify.sh`).**

### 3.1 Create the database

`deploy/schema.sql` is already written as "create database → create and grant the account → create
tables". Change its `'strong-password'` and run:

```bash
sudo mysql < deploy/schema.sql
sudo mysql -e "SHOW TABLES;" zabbix_ai_plugin          # should show sessions, audit_log
```

Choose one of the two privilege modes according to your DBA's requirements (details in 5.3):

| Mode | `config.yaml` | Account privileges | Who creates the tables |
|---|---|---|---|
| **A (recommended, default)** | `auto_init: true` | `SELECT INSERT UPDATE DELETE CREATE INDEX` | created automatically at application startup |
| **B (DBA-controlled)** | `auto_init: false` | `SELECT INSERT UPDATE DELETE` | created in advance by `deploy/schema.sql` |

> If the database is not on this machine, change the account host in `schema.sql`
> (`'zabbix_ai'@'127.0.0.1'`) to the application server's IP, and change `storage.mysql.host`
> in `config.yaml` at the same time.

### 3.2 Put the code in place and install the dependencies

```bash
sudo mkdir -p /opt/zabbix-ai-plugin
# put the contents of this repository into /opt/zabbix-ai-plugin (for the offline bundle, extract it there)
sudo chown -R zabbix:zabbix /opt/zabbix-ai-plugin
cd /opt/zabbix-ai-plugin
sudo -u zabbix python3 -m venv .venv
sudo -u zabbix .venv/bin/pip install -r requirements.txt
```

On networks in mainland China you can speed this up with a mirror:
`.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`

### 3.3 Write `.env` (all secrets live here)

```bash
cd /opt/zabbix-ai-plugin
sudo -u zabbix cp .env.example .env
sudo -u zabbix vi .env          # variable list in 5.2
sudo chmod 600 /opt/zabbix-ai-plugin/.env
```

### 3.4 Edit `config.yaml`

At a minimum change `zabbix.api_url` and `suppliers`; the full field table is in 5.1:

```yaml
zabbix:
  api_url: "http://your-zabbix-host:801/api_jsonrpc.php"   # ← must change

suppliers:                                               # ← change to your actual suppliers
  SUPPLIER_A:
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_SUPPLIER_A}"
    keyword: "告警"
```

### 3.5 Run it in the foreground and self-check

```bash
sudo -u zabbix .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 10801
curl -s http://127.0.0.1:10801/api/health
# expected: {"ok":true,"mysql":true,"llm_model":"glm-4-flash","zabbix_api_configured":true}
```

- `mysql` is `false` → check `MYSQL_PASSWORD` in `.env` and the `storage.mysql` settings
- Opening `http://127.0.0.1:10801/` directly in a browser reports "session expired" — **this is normal** (there is no Zabbix session)

> ⚠️ **The port comes from the start command**: `--port 10801` comes from systemd's `ExecStart` (see
> `deploy/zabbix-ai-plugin.service`), while `server.port` in `config.yaml` **is not used for
> listening** (see 10).

### 3.6 Install the systemd service

```bash
sudo cp /opt/zabbix-ai-plugin/deploy/zabbix-ai-plugin.service /etc/systemd/system/
sudo vi /etc/systemd/system/zabbix-ai-plugin.service   # adjust User/WorkingDirectory/EnvironmentFile/ExecStart as needed
sudo systemctl daemon-reload
sudo systemctl enable --now zabbix-ai-plugin
curl -s http://127.0.0.1:10801/api/health
```

### 3.7 Configure the nginx reverse proxy (two traps you will hit)

**`deploy/nginx.conf.example` is a snippet example and cannot be enabled as-is**
(`server_name`, certificates and `location /` are all placeholders). The right approach: add the
block below to **the very `server { }` block that is currently serving the Zabbix frontend** (the
port must match the Zabbix frontend):

```nginx
    location ^~ /ai/ {
        proxy_pass http://127.0.0.1:10801/;   # the trailing / is required: it strips the /ai prefix
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
```

> ⚠️ **You must write `location ^~ /ai/`, not `location /ai/`**: Zabbix's own configuration contains
> `location ~ /(api\/|conf[^\.]|include|locale) { deny all; return 404; }`,
> and this plugin's endpoints look like `/ai/api/health` (the path contains `api/`), so that rule
> matches them and returns 404. With `^~` the prefix match **skips the regex check**.

> ⚠️ **It must be same-origin with the Zabbix frontend (same domain + same port)**: the plugin tells
> who you are by the `zbx_session` cookie the browser sends automatically. Putting it in another
> server block (for example the default site on port 80) makes it cross-origin, the cookie is not
> sent, and the page keeps reporting "session expired". How to tell: run
> `ss -tlnp | grep nginx` and see which port serves the Zabbix frontend.

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -s http://your-zabbix-host:801/ai/api/health     # should return the same health JSON
```

### 3.8 Add the dialog to a dashboard

1. Log in to Zabbix → **Dashboards** → open the target dashboard → **Edit dashboard** → **Add** → **URL** widget
2. **Name**: `AI Assistant`; **URL**: `http://your-zabbix-host:801/ai/` (port matching the Zabbix frontend)
3. **Add** → **Save**; at least half a screen wide and one column tall is recommended

### 3.9 ⚠️ Required: turn off Zabbix's iframe sandbox restriction

Zabbix 6.0/7.0 has **Use iframe sandboxing** under **Administration → General → Other** (**enabled by
default**). While it is enabled and `Iframe sandboxing exceptions` is empty, the URL widget's iframe
carries `sandbox=""` and **scripts, same-origin and cookies are all disabled**. The symptoms are: the
dialog renders, but **typing `/` pops up no supplier names and clicking send does nothing**, and the
nginx log contains no `/ai/api/*` request at all.

**Fix**: set **Iframe sandboxing exceptions** to `allow-scripts allow-same-origin` (recommended), or
clear the **Use iframe sandboxing** checkbox (this applies to every URL widget and is less secure).
Command-line equivalent (replace `<API_TOKEN>` with your API token):

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"settings.update","params":{"iframe_sandboxing_exceptions":"allow-scripts allow-same-origin"},"auth":"<API_TOKEN>","id":1}' \
  http://your-zabbix-host:801/api_jsonrpc.php
```

> This setting applies globally to **all** URL widgets; if other URL widgets carry untrusted external
> content, assess the risk, or use the alternative: open `/ai/` as a standalone page (unaffected by
> iframe sandboxing, with identical functionality).
> See `DEPLOYMENT.md` 6.1.

### 3.10 Deployment acceptance checklist

- [ ] `curl http://127.0.0.1:10801/api/health` → `mysql: true`
- [ ] `curl http://your-zabbix-host:801/ai/api/health` → same as above
- [ ] the AI dialog is visible in the dashboard
- [ ] typing a single `/` in the input box → supplier names pop up below (if not, it is almost certainly the iframe sandbox issue from 3.9)
- [ ] type a sentence and click "send" → you get a reply (if nothing happens, same as above)
- [ ] visiting `/ai/` after logging out of Zabbix → it does not work (it reports an expired session, see 4.4)
- [ ] ask "what alerts are there recently" → real alerts come back
- [ ] `/SUPPLIER_A test message` → the DingTalk group receives the message

---

## 4. Usage

### 4.1 Natural-language Q&A

Just type your question; the plugin calls the matching tool to fetch real data. **Five** tools are
registered for the model:

| Tool | Purpose |
|---|---|
| `query_problems` | currently unresolved problems / historical failures in a time range (including recovered ones, with recovery time and duration); filterable by host |
| `query_top` | server-side aggregate ranking: which hosts/triggers alerted most in a time range (TOP N) |
| `query_metrics` | item values: the latest value, or min/max/avg/first/last over a time range; without `key` it returns the list of key families that actually exist on that host |
| `query_hosts` | host inventory and interface information (IP/port/DNS/interface type) |
| `trend_analysis` | linear extrapolation over trend data (e.g. "how many days until the disk is full") |

Typical questions:

| What you can ask | What the plugin does |
|---|---|
| `What alerts are there recently?` / `Is anything alerting right now?` | `query_problems` (scope=current) returns the **currently unresolved** problems |
| `Were there any failures this morning?` / `What went wrong yesterday?` | `query_problems` (scope=history) returns **all failures** in that range, including recovered ones |
| `What is the CPU usage on db01 right now?` | `query_metrics` returns the latest value of that host's item |
| `How many days until db01's disk is full? Capacity limit 500` | `trend_analysis` linear extrapolation |
| `Which machines alerted the most in the last 7 days?` / `TOP10 devices by alerts this month` | `query_top`: server-side aggregation, ranked by host/trigger |
| `What is the IP of DEMO_CARRIER01?` / `What port does db01 use?` | `query_hosts` (with `search`) returns `ip`/`port`/interface type; hosts with `useip=0` return the DNS name |
| `Analyze DEMO_SDWAN01's condition this month` | `query_problems` (with `host`) counts only that host, then `query_metrics` aggregates min/max/avg |
| `Bandwidth usage on DEMO_SDWAN02 during the alert window` | `query_metrics` (`host` + `key` + `period`) gives the statistics for that range |
| `Which items does db01 have?` (when you do not know the key) | `query_metrics` **without `key`**: returns the list of key families that really exist (with units and sample keys) that you can copy and retry |

**You do not have to remember item keys**: agent hosts and SNMP devices use completely different key
naming (the former like `system.cpu.load`, the latter like `net.if.in[ifHCInOctets.15]`).
When you are unsure of the key, ask without a `key` and the plugin first lists the items that
**really exist** on that host; when it cannot find a key it does **not** invent one, and it does
**not** report a missed guess as "no data".

**Supported time periods** (these 14 are the valid values of the tool parameter; the model maps what
you said onto them):

| Key | Meaning | Key | Meaning |
|---|---|---|---|
| `today` | today 00:00 until now | `last_night` | last night 18:00-24:00 |
| `early_morning` | today 00:00-06:00 | `this_month` | the 1st of this month 00:00 until now |
| `morning` | today 00:00-12:00 | `last_month` | the 1st of last month 00:00 to the 1st of this month 00:00 |
| `noon` | today 11:00-14:00 | `last_1h` | the last 1 hour |
| `afternoon` | today 12:00-18:00 | `last_24h` | the last 24 hours |
| `evening` | today 18:00-24:00 | `last_7d` | the last 7 days |
| `yesterday` | all of yesterday | `last_30d` | the last 30 days |

These are the Chinese colloquial words the server **also recognizes on its own** (matched in order,
longer words first):

```
昨天晚上 昨晚 昨天下午 昨天上午 今天中午 今天下午 今天上午 今天凌晨 今天晚上
凌晨 半夜 深夜 清晨 早晨 早上 上午 中午 正午 下午 午后 傍晚 晚上 晚间 夜里
前天 昨天 昨日 今天 今日 本日
```

> **The answer always states the time range actually queried**, so you can see at a glance whether it
> looked at "today noon 11:00-14:00" or "the last 24 hours". If a period was not recognized it
> **says so** (`⚠ 未能识别时段 'X'`, i.e. "period 'X' not recognized") and does **not** quietly
> substitute a different range.
> Note: Chinese **duration** words ("最近7天", "最近1小时") are not in the colloquial list above;
> phrasings like these rely on the model mapping them to keys such as `last_7d`. "昨天夜里" is
> currently a trap, see 10.
> Historical failures come from the Zabbix events table, whose retention is decided by housekeeping
> (365 days is common); they are counted **by event occurrence time** and do not mean something is
> still alerting right now.

### 4.2 The `/supplier-name` command (send a problem to a supplier)

**Where are the names defined?** Under `suppliers:` in `config.yaml`, **every key name is one
supplier** — that is the name you write after the slash:

```yaml
suppliers:
  网络供应商A:            # ← this name is what you type as /网络供应商A
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_NETWORK_A}"
    keyword: "告警"
  机房供应商B:            # ← the second supplier, used as /机房供应商B
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_IDC_B}"
```

**How many can I configure?** Any number. But typing `/` lists **at most 10** candidates below the
input box (see 10); with more suppliers, keep typing a prefix to filter them.

**Syntax**: `/supplier-name message text`, for example
`/网络供应商A Severe packet loss on the uplink, please help investigate; contact Zhang San 138xxxx`

**Is there autocomplete?** Yes: typing a single `/` in the input box lists the configured suppliers
and **highlights the first one by default**, so you never need to touch the mouse.

| Key | Action |
|---|---|
| `↑` / `↓` | move between candidates (wrapping at the ends) |
| `Enter` or `Tab` | accept the highlighted entry, fill in `/supplier-name ` and collapse the list |
| `Esc` | collapse the list |

After completion, keep typing your message and press `Enter` to send — **once you type a space (i.e.
start writing the body), `Enter` goes back to meaning send** and is no longer stolen by the
completer. Clicking a candidate with the mouse works as well.

> When only **one** candidate matches (for example you already typed `/SUPPLIER_A`), the arrow keys
> are **not captured** — selection is meaningless then, `Enter`/`Tab` completes directly, and the
> arrow keys keep working as normal cursor movement.

The hint bar at the bottom permanently shows the list of currently available suppliers.

**Prefix matching rules**

- A name may be abbreviated as long as the match is **unique**: with "网络供应商A" configured, `/网络` works too;
- When a prefix matches **several** names the send is **refused** and the available names are listed, so you never post to the wrong group;
- A name that is **not on the whitelist** is always refused, with the list of available names returned.

**What happens after sending**

- Message format: `[zabbix-AI] 发送人 上报：` + the message body (configurable, see `dingtalk.message_template` in 5.1)
- Success: the dialog reports the successful send, the target supplier and the send time; failure: DingTalk's error message (such as `errcode=310000`) is shown verbatim and written to the audit log
- **Sending is executed deterministically by code and does not depend on AI judgement**: if it says "sent", the DingTalk API really did return success
- **It cannot be triggered in natural language**: the AI has **no** send tool. For a request like "send a message to a supplier for me", the plugin points you to `/supplier-name message text` and does **not** send it on your behalf. This is deliberate: sending has real external side effects, and leaving it to the model's own judgement could both claim a send that never happened and push monitoring-unrelated content into a supplier group.
- Typing only `/` or an incomplete form returns usage instructions plus the list of available suppliers, and sends nothing

**How do I configure the DingTalk robot?**

1. DingTalk group → Group settings → Smart group assistant → Add robot → **Custom** → copy the Webhook URL
2. Pick one of the three security settings and fill in `config.yaml` as in the table:

| Robot console setting | What to put in `config.yaml` |
|---|---|
| **Custom keyword** (recommended, simplest) | `keyword: "告警"`, **exactly matching** the keyword in the console |
| **Signing** | `secret_env: "DINGTALK_NETWORK_A_SECRET"`, with the secret in the matching `.env` variable |
| **IP whitelist** | nothing to fill in; just whitelist the server's outbound public IP |
| **No security setting** (token only) | fill in neither; the `webhook` alone is enough (verified working) |

```yaml
suppliers:
  网络供应商A:
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_NETWORK_A}"
    keyword: "告警"                              # keyword mode
    # secret_env: "DINGTALK_NETWORK_A_SECRET"    # signing mode (can coexist with keyword)
    # message_template: "【来自监控】{message}"   # optional: a separate message template for this supplier

  # without a security setting (no keyword / secret_env)
  SUPPLIER_A:
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_SUPPLIER_A}"
```

**The message body format** (default `[zabbix-AI] {username} 上报：\n{message}`) can be customized
through configuration alone, with no code changes:

```yaml
dingtalk:
  message_template: "[zabbix-AI] {username} 上报：\n{message}"
# body only (drop the "someone reports" part):  message_template: "{message}"
```

Placeholders: `{username}` is the sender, `{message}` is the message body. A per-supplier
`message_template` takes precedence over the global one; restart the service for the change to take
effect. Compatible shorthand: `supplier-name: "https://...webhook..."` giving the URL string
directly (no keyword, no signing).

**Common DingTalk errors**

| errcode | Cause | What to do |
|---|---|---|
| `310000` | keyword mismatch (`keywords not in content`) | make `keyword` match the robot console |
| `310000` | signature verification failed (`sign not match`) | configure `secret_env` and put the secret in `.env` |
| `310000` | the sender IP is not whitelisted | whitelist the server's outbound IP, or switch to keyword/signing |
| `300001` | invalid `access_token` | check the token in `.env` |
| `40035` | empty token | a `DINGTALK_*` variable in `.env` is empty, or its name does not match the `${...}` in `config.yaml` |

### 4.3 Floating chat ball (optional)

If you do not want the dialog to occupy dashboard grid space, you can make it a **floating ball in
the bottom-right corner**: clicking it pops up a small panel with the chat embedded inside,
**without navigating or refreshing**; the ball can be dragged and its position is remembered in
`localStorage`.

```bash
# run this on the machine that serves the Zabbix frontend (root required)
sudo python3 deploy/install_launcher.py                  # install (idempotent, safe to re-run)
sudo python3 deploy/install_launcher.py --status         # show status
sudo python3 deploy/install_launcher.py --uninstall      # uninstall
sudo python3 deploy/install_launcher.py --config /etc/nginx/conf.d/zabbix.conf   # specify the nginx config (this path is the default)
sudo python3 deploy/install_launcher.py --no-check       # skip the post-install self-check
```

What it does: it adds 3 `sub_filter` lines to nginx's PHP location, injecting
`<script src="/ai/launcher.js" defer></script>` before `</body>` of every HTML page.
**Not a single Zabbix file is modified**, so `apt upgrade zabbix-frontend-php` does not overwrite
this change; revert it with `--uninstall` (the script backs up before changing anything, and rolls
back automatically if `nginx -t` fails).

Where it shows up, and permissions:

- **It appears only on dashboard pages while you are logged in**: `launcher.js` checks the URL for
  `action=dashboard.view` and the profile entry on the page, and **creates no ball at all** on the
  login page, the alert list and similar pages
- the iframe loads `/ai/` same-origin, so the browser sends `zbx_session` automatically and **everyone still sees only the data their permissions allow**
- the ball and the URL widget can coexist (both entries point at the same chat page)

> Debugging: `curl -s http://<frontend>:801/zabbix.php?action=dashboard.view | grep launcher.js`
> should show the injected script tag; fetching `/ai/launcher.js` directly should return JS.
> Note: the installer's **self-check hardcodes `127.0.0.1:801`**, so it fails when the Zabbix
> frontend is not on 801 (skip it with `--no-check`).

### 4.4 Sessions, endpoints and auditing

**HTTP endpoints** (forwarded by nginx to this app; the browser-side prefix is always `/ai/` — note that `server.base_path` is inert, see 10):

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | none (returns 200 HTML) | the chat page itself. **The page opening does not mean you are logged in**: the frontend then calls `/api/*`, and on a 401 it clears the UI and reports "session expired" |
| `GET` | `/api/health` | none | self-check: `{"ok","mysql","llm_model","zabbix_api_configured"}` |
| `GET` | `/license` | none | **full license text** (GNU GPL v3.0); the "About" entry in the UI links here |
| `POST` | `/api/chat` | required | submit a turn: `{"message": "...", "clear": false}` → `{"reply": "...", "context_reset": bool}` |
| `GET` | `/api/history` | required | current session history, available suppliers, `ttl_minutes` |
| `GET` | `/api/audit` | required (**roleid=3, super administrator**) | audit records: who, when, sent to which supplier |

Without a valid `zbx_session`, the endpoints above that require authentication return **401**; when
the Zabbix API is unreachable they return **503** (deliberately distinguished: a 503 is not misread
as "you are not logged in").

**Conversation history is isolated per login session, not per user:**

- **Logging out and back in means a brand-new conversation**: Zabbix issues a new `sessionid` on every login and the plugin uses it as the conversation key; the same account in two browsers also gets two independent conversations.
- **An idle timeout clears it automatically**: when the last message is more than `history.ttl_minutes` ago (default **15 minutes**), the next open or question starts a new session and deletes the old records. The UI reports "the previous conversation was idle for more than N minutes, a new session has started", so what you see and what the model remembers cannot drift apart.
- Clicking "clear session" in the top-right corner clears the current session at any time.
- Fallback cleanup: records older than `history.retention_days` (default 7 days) are deleted at service startup.
- **Audit log**: every DingTalk send (including failures) records the sender, the supplier, the time and a digest of the content.

### 4.5 Theme (colors follow Zabbix)

The dialog **follows the Zabbix theme** by default: it reads the parent page's theme class
(`theme-blue` / `theme-dark` and so on) and switches between light and dark automatically, so you
never get the jarring "white Zabbix, black plugin" mix.

- the **Theme** button inside the widget switches between `follow / light / dark` (the choice is stored in the browser)
- you can also force it through the URL: `http://your-zabbix-host:801/ai/?theme=light`
- when the page is opened standalone and no parent theme can be read, it falls back to the system preference (`prefers-color-scheme`)

---

## 5. Configuration Reference

### 5.1 Full `config.yaml` field table

Secrets (API Key / database password / access_token / signing secret) are always injected through
environment variables and are **never written into this file**.

| Field | Description |
|---|---|
| `zabbix.frontend_url` | frontend entry URL, **display only** (builds the same-origin path hint); no code reads it to make decisions |
| `zabbix.api_url` | **must change**. Zabbix JSON-RPC URL, usually `http://domain:801/api_jsonrpc.php` |
| `zabbix.auth.mode` | keep `user`: pass through the logged-in user's session so permissions stay isolated per user. **`service` is not wired up yet**, see 10 |
| `llm.base_url` | LLM endpoint (OpenAI-compatible), see 5.4 |
| `llm.model` | model name, see 5.4 |
| `llm.api_key_env` | **name of the environment variable** holding the API key (not the key itself) |
| `llm.temperature` | sampling temperature, default `0.3` (tool calling needs determinism; raising it is not recommended) |
| `llm.top_p` | sampling top_p; the repository's `config.yaml` uses `0.9`; **delete that line and it falls back to the code's built-in default of `1.0`** |
| `llm.max_tokens` | per-reply cap, default `1024` |
| `llm.function_calling` | **must stay `true`**. With `false` the request sends **no tools at all** and all 5 tools are unavailable (the model can only answer from its own knowledge). There is no "prompt-style parsing" fallback path in the code |
| `llm.extra_params` | extra parameters passed through to the backend (fill in according to what the backend supports; empty by default) |
| `dingtalk.message_template` | DingTalk message body template, default `[zabbix-AI] {username} 上报：\n{message}`; placeholders `{username}`/`{message}` |
| `suppliers` | supplier whitelist; the key name is the name used after `/`, see 4.2 |
| `server.host` / `server.port` / `server.base_path` | **currently inert** (parsed but used by nothing). The listen address and port come from systemd's `ExecStart`; the path prefix comes from nginx. See 10 |
| `history.ttl_minutes` | how long idle before it counts as a new session, default `15`; `0` = never clear automatically |
| `history.max_messages` | cap on how many history messages are sent to the model, default `50` |
| `history.retention_days` | fallback cleanup: delete records older than this many days at startup, default `7`; `0` = no cleanup |
| `storage.mysql.host` / `storage.mysql.port` / `storage.mysql.database` / `storage.mysql.user` / `storage.mysql.charset` | database connection parameters (defaults `127.0.0.1` / `3306` / `zabbix_ai_plugin` / `zabbix_ai` / `utf8mb4`) |
| `storage.mysql.password_env` | name of the environment variable holding the database password, default `MYSQL_PASSWORD` |
| `storage.mysql.auto_init` | `true` creates the tables at application startup (needs CREATE privilege); `false` leaves them to be created by the DBA. See 5.3 |

### 5.2 Full `.env` variable table

| Variable | Purpose |
|---|---|
| `LLM_API_KEY` | the LLM's API Key (**required**) |
| `MYSQL_PASSWORD` | database password (**required**, matching `schema.sql`) |
| `DINGTALK_NETWORK_A` / `DINGTALK_IDC_B` | access_token of each supplier group robot (as needed; the name must match the `${...}` in `config.yaml`) |
| `DINGTALK_NETWORK_A_SECRET` / `DINGTALK_IDC_B_SECRET` | signing secrets (needed in signing mode only) |
| `ZABBIX_SERVICE_USER` / `ZABBIX_SERVICE_PASSWORD` | **currently ineffective** (`auth.mode=service` is not wired up, see 10) |

> `.env.example` is the template **committed to the repository** and holds placeholders only;
> `.env` is ignored by `.gitignore` and should have mode `600`.

### 5.3 The two database privilege modes

| Mode | `auto_init` | Account privileges | Who creates the tables |
|---|---|---|---|
| A (recommended, default) | `true` | `SELECT INSERT UPDATE DELETE CREATE INDEX` | `CREATE TABLE IF NOT EXISTS` at application startup |
| B (DBA-controlled) | `false` | `SELECT INSERT UPDATE DELETE` | the DBA creates them in advance per `deploy/schema.sql` |

Two tables: `sessions` (conversation history, keyed by login session id) and `audit_log` (DingTalk
send audit). **The plugin stores only its own data and needs no access to the Zabbix database**, so
it uses a dedicated database plus a dedicated least-privilege account.

### 5.4 LLM configuration

**Repository defaults** (Zhipu's official endpoint, an OpenAI-compatible API):

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

**Switching model / vendor**: change only `base_url` and `model`; no code changes are needed.

| Vendor | `base_url` | `model` example |
|---|---|---|
| Zhipu | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| SiliconFlow | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |

**Hard requirement**: the chosen model must support **Function Calling**, otherwise none of the tools
work. Also note: the per-request LLM timeout is **hardcoded to 60 seconds** (`app/llm.py`) and cannot
be adjusted through configuration.

**How to debug LLM connectivity**:

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

If `tool_calls` appears in the second response, the model supports tool calling; if it returns plain
text only, you need a different model.

---

## 6. Upgrades and Routine Operations

```bash
systemctl status zabbix-ai-plugin            # status
journalctl -u zabbix-ai-plugin -n 100 -f     # logs
sudo systemctl restart zabbix-ai-plugin      # restart (required after changing config.yaml / .env)
```

- After upgrading the code: `sudo -u zabbix .venv/bin/pip install -r requirements.txt` (when dependencies changed) → restart
- Upgrading the Zabbix frontend does **not** affect this plugin (the plugin modifies no Zabbix file; the only exception is the optional nginx `sub_filter` injection, which edits the nginx configuration rather than Zabbix files and can be undone with `--uninstall`)
- The database tables are maintained by `CREATE TABLE IF NOT EXISTS`, so upgrading loses no data

**For production upgrade / rollback / data-retention / backup procedures see Section 11 of
`DEPLOYMENT.md`** (rollback points, `app.bak.<version>` backups, and how to handle unbounded growth
of the `history` table).

---

## 7. Troubleshooting and FAQ

| Symptom | Cause and what to do |
|---|---|
| The page says "session expired, please log in to zabbix first" | there is no valid `zbx_session` cookie. Make sure you entered from the dashboard and that `/ai/` is on the **same domain and port** as the Zabbix frontend; cross-origin requests do not send cookies |
| `mysql: false` in `/api/health` | `MYSQL_PASSWORD` in `.env` differs from `schema.sql`, or `storage.mysql.user/host` is wrong, or the account host does not match where the connection comes from |
| A question gets no response / reports "(service error)" | the LLM Key is invalid or out of quota. Verify connectivity with the script in 5.4 first |
| The answer is always "tool call limit reached" | the model's Function Calling ability is weak; switch to a stronger model |
| DingTalk reports `errcode 310000` | see the error table in 4.2 (keyword / signing / IP whitelist) |
| `/supplier-name` reports "supplier not registered" | the name is not under `suppliers` in `config.yaml`; the name must match exactly, or use a unique prefix |
| `/ai/` returns an nginx 404 | ① is it written as `location ^~ /ai/` (without `^~`, Zabbix's `api/` regex intercepts it); ② the trailing `/` on `proxy_pass` is required; ③ did you reload nginx |
| **The dialog renders, but typing `/` shows no suggestions and clicking send does nothing** | **Zabbix's default iframe sandbox disables page scripts** — set `Iframe sandboxing exceptions` as described in 3.9. Self-check: if the nginx log shows **no** `/ai/api/*` at all after you click send, this is the cause |
| Colors do not match Zabbix | click **Theme** in the widget's top-right corner and switch to **follow**; or force it with `?theme=light`; older frontends are hardcoded dark and need upgrading |
| Port conflict (10801 already in use) | change **two places**: `ExecStart --port` in systemd and `proxy_pass` in nginx. **Changing `server.port` in `config.yaml` has no effect at all** (see 10) |
| A database privilege error at startup | the account privileges do not match `auto_init`: `auto_init: true` needs `CREATE`; with DML privileges only, set `auto_init: false` and let the DBA create the tables |
| The log shows "table schema self-check failed" | at startup the plugin verifies that the `sessions`/`audit_log` structures match what it expects; rebuild or fix them with `deploy/schema.sql` as the message suggests |

---

## 8. Development and Testing

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -v
```

How to run it, and the baseline:

```bash
python -m pytest -q          # all green is enough; the number of cases grows with versions and is not hardcoded in the docs
```

The skipped cases are those that "need a real MySQL" or where "the `dist/` offline bundle does not exist" — **the exact count is deliberately not written down here**, since it grows with every released offline package.
**The specific numbers are not hardcoded in the documentation**: every released offline bundle adds
parameterized cases (3 per bundle), and a clean clone collects a different count from a development
machine that has `dist/`.

| Test file | Coverage |
|---|---|
| `tests/test_api.py` | routing and authentication (401 when not logged in, 503 when Zabbix is unreachable), `/api/chat`/`/api/history`/`/api/audit` |
| `tests/test_auth.py` | `zbx_session` parsing (URL decoding + base64 JSON), expired/malformed cookies |
| `tests/test_config.py` | `config.yaml` parsing, the supplier whitelist and ambiguous prefixes |
| `tests/test_tools.py` | parameter validation for the 5 tools, period parsing, key-family relaxation, trend extrapolation (about 80 cases) |
| `tests/test_llm.py` | LLM request body, the tool-calling loop, the round limit |
| `tests/test_dingtalk.py` | the signing algorithm, keyword injection, template rendering |
| `tests/test_storage_mysql.py` | MySQL session isolation and audit round-trip (needs a real database, see below) |
| `tests/test_package.py` | offline bundle and `.sha256` consistency |
| `tests/test_shipped_config.py` | the `config.yaml` shipped with the repository is not broken |
| `tests/test_launcher_js.py` + `tests/js/*.test.js` | frontend JS behaviour self-tests (`node` plus a minimal DOM stub, driven by pytest) |
| `tests/test_docs.py` | **documentation/code consistency**: every tool name, config field, environment variable, period and file listed in this document must match the code |
| `tests/integration/` | the Q&A verification suite against a **real Zabbix** (needs a test server, see its `README.md`) |

Cases that need MySQL (optional, using a separate test database):

```bash
TEST_MYSQL_HOST=127.0.0.1 TEST_MYSQL_USER=zabbix_ai TEST_MYSQL_PASSWORD=xxx \
TEST_MYSQL_DB=zabbix_ai_plugin_test python -m pytest tests/test_storage_mysql.py -v
```

Q&A verification against a real server (`tests/integration/`) additionally needs: `QA_ZABBIX_API`,
`QA_ZABBIX_USER`, `QA_ZABBIX_PASSWORD`, `QA_PLUGIN` (the plugin URL) and `QA_SERVICE` (the systemd
service name); see `tests/integration/README.md` for environment preparation and data generation.

Change conventions are in `AGENTS.md`: commit every change, and make sure the whole test suite passes
before delivery.

---

## 9. Directory Structure

```
zabbix-ai-plugin/
├─ app/
│  ├─ __init__.py          # package marker
│  ├─ main.py              # FastAPI entry point, routes, health check, slash command
│  ├─ config.py            # config.yaml + environment variable loading
│  ├─ auth.py              # zbx_session cookie validation (401 / 503 semantics)
│  ├─ zabbix.py            # Zabbix JSON-RPC client (session pass-through)
│  ├─ llm.py               # LLM abstraction layer + tool-calling loop
│  ├─ tools.py             # the 5 tool implementations + registry + period parsing
│  ├─ dingtalk.py          # DingTalk sending (signing / keyword / template)
│  └─ storage.py           # MySQL: conversation history + audit
├─ frontend/
│  ├─ index.html           # single-page chat UI (theme following, / command hints, keyboard-first selection)
│  └─ launcher.js          # floating chat ball (nginx injection, see 4.3)
├─ deploy/
│  ├─ schema.sql                  # create database / grants / create tables
│  ├─ nginx.conf.example          # nginx snippet example (copy and edit, see 3.7)
│  ├─ zabbix-ai-plugin.service    # systemd service (the port lives here, see 3.6)
│  ├─ preflight.sh                # pre-deployment environment check
│  ├─ verify.sh                   # post-deployment self-check
│  ├─ package.py                  # build the production offline bundle
│  └─ install_launcher.py         # install/uninstall the floating chat ball
├─ tests/
│  ├─ test_api.py  test_auth.py  test_config.py  test_dingtalk.py
│  ├─ test_llm.py  test_storage_mysql.py  test_package.py
│  ├─ test_shipped_config.py  test_launcher_js.py  test_tools.py
│  ├─ test_docs.py                 # documentation/code consistency checks
│  ├─ js/                          # frontend JS self-tests (launcher.test.js, suggest.test.js)
│  └─ integration/                 # Q&A verification suite against a real Zabbix (qa_bank.py question bank, qa_fixture.py data generation)
├─ config.yaml             # configuration (no secrets)
├─ .env.example            # secret template → copy to .env
├─ requirements.txt / requirements-dev.txt
├─ .gitignore / .gitattributes     # .gitattributes pins LF (keeps .sh from failing on Linux over CRLF)
├─ conftest.py             # shared pytest fixtures
├─ README.md               # Chinese README (the original of this document)
├─ README.en.md            # English README (this document)
├─ DEPLOYMENT.md           # production deployment documentation
├─ RECOVERY-IDC.md         # on-site IDC recovery checklist
├─ SECURITY.md             # security policy and vulnerability reporting
├─ CHANGELOG.md            # version history
├─ LICENSE                 # full text of GNU GPL v3.0
└─ AGENTS.md               # change conventions
```

---

## 10. Known Limitations

These are limitations that **really exist in the current version**; they are written down here so the
documentation never promises what the code cannot do:

1. **`zabbix.auth.mode: service` is not wired up**: authentication always requires the user to be logged in to Zabbix, and data queries always use the user session (every method of `ZabbixClient` passes the user's `sessionid` through and never uses the service account). `ZABBIX_SERVICE_USER/PASSWORD` are therefore read but useless, and the comment in `app/auth.py` saying that "service mode only affects which account data queries use" does not match the facts. **For now, use `user` mode only.**
2. **`server.host` / `server.port` / `server.base_path` have no effect**: these three fields are parsed and never used. The actual listen address and port are decided by the start command (the `--host/--port` of systemd's `ExecStart`), and the path prefix is decided by nginx. To resolve a port conflict, change both systemd and nginx.
3. **`llm.function_calling: false` is not a "degraded mode"**: with `false` the request sends no tools at all and all 5 tools are unavailable; `try_parse_tool_call()` in `app/llm.py` is an experimental function that **is never called by production code** (covered by unit tests only), so there is no "prompt-style parsing" fallback.
4. **"昨天夜里" is parsed incorrectly**: in the Chinese colloquial word list, `夜里` (today 18:00-24:00) is matched before the date words, so "昨天夜里" is taken to mean **today** 18:00-24:00. Say "昨晚" for now. Likewise "上月" is currently not recognized by the Chinese colloquial word list (use "本月" or have the model map it to `last_month`).
5. **Chinese duration words are not parsed by the server**: phrasings like "最近7天" and "最近1小时" depend on the **model** mapping them to `last_7d` / `last_1h`; if the model passes the Chinese through into the tool parameters verbatim, the plugin answers plainly that the period was not recognized and queries the last 1 day instead (it does not pretend to have used the window you asked for).
6. **At most 10 supplier candidates are shown**: `config.yaml` may define any number of suppliers, but typing `/` lists only the first 10 matches in the frontend; with more, keep typing a prefix to narrow them down.
7. **The LLM request timeout is hardcoded to 60 seconds** and is not configurable; when the model does not respond for a long time this shows up as a wait followed by a failure.
8. **`GET /` returns 200 even for unauthenticated requests**: the page itself is not authenticated; it relies on the frontend calling `/api/*`, getting a 401 and then reporting "session expired". The real data endpoints always return 401, so there is no unauthorized data access.
9. **`zabbix.frontend_url` is display-only**: no code reads it for any decision, so changing it does not affect the access path.

> If you want one of the entries above to move from "known limitation" to "fixed", that is a code
> change (it needs a new release + deployment); adding a reproducing test case under `tests/` first
> is recommended.

---

## License

This project is released under the **GNU General Public License v3.0**; the full text is in
[`LICENSE`](LICENSE) — it is also reachable from the "**About**" entry in the top-right corner of
the UI (`GET /license`, no login required).

Copyright (C) 2026 **shourenli**.

- You are free to use, modify and redistribute this project, including for commercial purposes;
- but **when you distribute a modified version or a derivative work you must release it under GPL-3.0 as well** (copyleft) and provide the corresponding source code;
- the software is provided "as is", without any warranty.

If you need to integrate it into a closed-source product or require other licensing terms, contact
the repository owner to discuss.

> Note: GPL-3.0 covers the source code, scripts and documentation in this repository. `.env`,
> secrets, production configuration and runtime data are not part of this repository and are not
> distributed with this project (see [`SECURITY.md`](SECURITY.md)).
