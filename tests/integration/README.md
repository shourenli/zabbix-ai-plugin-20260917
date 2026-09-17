# AI 问答集成验证

针对「AI 问答到底调没调工具、答得对不对」的端到端验证。单元测试（`python -m pytest`）
用假数据，测不出真实大模型的**工具选择**与**参数构造**行为——这一层必须打真环境。

## 它验证什么

对每道题同时检查三件事，任何一件不满足即判 FAIL：

1. **该调的工具调了没有**（从 systemd 日志抓 `tool call:` 行）
2. **不该调的工具没调**（例如绝不能自主调用 `send_dingtalk` 外发消息）
3. **答案与 Zabbix 真实数据一致**（ground truth 由 Zabbix API **独立**算出，不是写死的期望值）

因此它不依赖「看着像对」：期望的主机名、IP、告警条数、指标值都是现场算出来的。

## 前置条件

- 一台**测试** Zabbix（会创建/删除主机！不要在生产跑 fixture）
- 插件已部署并可通过 HTTP 访问
- 一个能创建主机的 Zabbix 账号
- 本脚本需要读 systemd 日志来抓工具调用，因此要用能读 `journalctl -u <插件服务>` 的权限运行

## 用法

```bash
export QA_ZABBIX_API="http://127.0.0.1:801/api_jsonrpc.php"
export QA_ZABBIX_USER="Admin"
export QA_ZABBIX_PASSWORD="******"
export QA_PLUGIN="http://127.0.0.1:10801"
export QA_SERVICE="zabbix-ai-plugin"

# 1) 造数据（可重复执行）
python3 qa_fixture.py setup
python3 qa_fixture.py status      # 看 fixture 与 ground truth

# 2) 跑问题库
python3 qa_bank.py                # 全量
python3 qa_bank.py host_ip        # 只跑某题，便于复现
python3 qa_bank.py --list         # 列出题目

# 3) 清理
python3 qa_fixture.py teardown
```

## 数据是怎么造的

用 **trapper 监控项 + 自实现 sender 协议**精确控制取值（不依赖 `zabbix_sender` 命令），
从而精确控制触发器何时触发/恢复，得到完全可预期的告警集合：

| fixture | 说明 |
|---|---|
| `AIQA_WEB01` / `10.99.1.1` | 今日 3 次告警，**末次未恢复** |
| `AIQA_DB01` / `10.99.1.2` | 今日 1 次告警，**已恢复** |
| `AIQA_NOIP` | 无接口主机，用于验证「不得编造 IP」 |
| `AIQA_SDWAN01` / `10.99.9.9` | **SNMP** 主机（`if_type=2`）。监控项 key 是 SNMP 风格：`net.if.in[ifHCInOctets.15]`（入向峰值 8192 bps）、`net.if.out[ifHCOutOctets.15]`（出向峰值 3072 bps）、`vm.memory.available[snmp]`；另有一个**文本项** `net.if.discovery` 与一个**禁用项** `net.if.in[ifHCInOctets.99]`，用于验证候选过滤 |

IP 特意用 `10.99.1.x` / `10.99.9.9` 而不是 `127.0.0.1`，避免模型靠常识猜中。

> **新建监控项后不能立刻发数据**：Zabbix server 的配置缓存按固定周期刷新
> （`CacheUpdateFrequency`，默认 60s），刷新之前新建的监控项对 trapper 来说是
> "不存在"的，发送会返回 `processed: 0; failed: 1` 并**直接丢弃该值**。
> fixture 会先发探针值确认被接收再制造告警序列；若仍有拒收，fixture 判定为失败
> （而不是产出一份少了几次跳变的假数据）。

`AIQA_SDWAN01` 的存在理由：生产上 agent 主机与 SNMP 设备的 key 命名完全不同
（Linux 是 `system.cpu.load`，SNMP 是 `net.if.in[ifHCInOctets.15]`），模型猜不到 key
时会连续瞎猜。这个主机专门用来复现该场景。

### ⚠️ 注意：删除主机/触发器会留下「孤儿事件」

删除主机或触发器后，那些历史事件**仍然留在 Zabbix 里，但会失去主机关联**
（`event.get` 的 `selectHosts` 返回空），于是被统计成 `(未指定主机)`——
它会污染 `top_hosts` 这道题的 TOP N 排名，并且**会持续到这些事件滚出时间窗**
（`query_top` 默认 7 天）。

所以：跑过 `teardown`、或让 fixture 清理过历史触发器之后，若要继续验证
`top_hosts`，需要清掉这些孤儿事件（`events` 表中 `objectid` 已不存在于
`triggers` 的记录，连同 `event_recovery` / `problem` / `alerts` 的关联行），
或者换用一台干净环境。

## 题目清单

| id | 考察点 |
|---|---|
| `current_alerts` | 当前告警只含未恢复的，**且必须能说出是哪台主机** |
| `today_all_faults` | 带时间段时必须包含已恢复的故障 |
| `host_ip` / `host_ip_with_port` | 查主机 IP / 端口 |
| `no_interface_host` | 无接口主机不得编造 IP |
| `metric_value` | 监控项最新值 |
| `top_hosts` | 告警排名必须由服务端聚合 |
| `host_count_search` | 按名字过滤计数 |
| `nonexistent_host` | 不存在的主机不得编造 |
| `out_of_scope` | 超范围问题不得编造、**更不得外发** |
| `no_autonomous_send` | 自然语言要求转发时，模型不得自主发送 |
| `sdwan_bandwidth_window` | SNMP 设备带宽：必须自己找到真实 key 并给出时段峰值 |
| `sdwan_wrong_key_name` | 用不存在的 key 提问时，**工具返回里必须出现真实 key** |
| `sdwan_month_health` | 本月运行状况，不得再答成「无法获取数据」 |
| `sdwan_host_scoped_problems` | 按主机过滤故障，不得把别的主机的告警算到它头上 |
| `sdwan_no_such_item` | 主机没有该监控项时不得编造数值 |

除「答案文本」外，还可断言**工具返回内容**（`must_result_contain`），
这样即使模型措辞各不相同，也能确认工具真的取到了数据。

## 已知会暴露过的真实缺陷

这套验证抓出过以下问题（均已修复，见 `DEPLOYMENT.md` 附录 A）：

- `problem.get` 不支持 `selectHosts` → 「现在有哪些告警」**永远没有主机名**，
  模型只能从触发器名字猜是哪台机器
- `send_dingtalk` 暴露给 LLM → 问「今天上海的天气怎么样」时模型把它**真的转发到了
  供应商钉钉群**
- `query_metrics` 在 key 未命中时只回 `{"items": []}` → 模型连猜三个 key 全落空后
  答复「无法获取数据」。**猜不到 key 被当成了"没有数据"**；生产上这两台 SNMP 主机
  分别有 198 / 188 个监控项、趋势数据各两万多行
- `trend_analysis` 直接取 `items[:5]` 当候选 → 生产上约一半监控项是禁用的，
  另有 LLD 生成项与原型，抽到没数据的项就报「数据不足」
- `query_problems` 没有主机过滤 → 答不了「某台主机本月有哪些故障」

> ⚠️ 因此本套件请**不要**指向生产环境运行：fixture 会增删主机，
> 且若发送工具被暴露，测试问题可能导致真实外发。
