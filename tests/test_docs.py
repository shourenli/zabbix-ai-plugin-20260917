# -*- coding: utf-8 -*-
"""文档与代码一致性检查 + 仓库敏感信息守卫。

为什么需要这个文件：

1. **文档漂移**：README 曾在 18 个提交里逐次追加，结果是同一份文档里「四个工具」与
   「五个工具」并存、架构图还画着已经收掉的发送工具、目录树缺 6 类文件。这类漂移靠人眼
   在 review 时抓不住，所以把它变成测试：**文档里出现的名字必须与代码一致**。
2. **真实资产泄露**：本仓库曾把真实生产主机名、公网 IP、组织名写进文档/注释/测试
   （第三方审计 M-5）。清理之后必须防止再次流入，因此这里做一次**全仓形态扫描**：
   真实名称不写在守卫里，而是以「词哈希 + 形态」表达，仓库本身不含任何真实标识。

检查项：
  1. README 里提到的工具名 == app/tools.py 注册给模型的工具名
  2. send_dingtalk 不得出现在注册表里，且 README 必须写明「AI 没有发送工具」
  3. config.yaml 的每个叶子字段都出现在 README 里
  4. .env.example 的每个变量都出现在 README 里
  5. app/tools.py 的每个时段键都出现在 README 里
  6. 仓库里的源文件/文档在 README 的目录结构一节里都有名字
  7. README 标注的版本 == 最新的 git tag
  8. 同文件内的 (#锚点) 链接必须能解析
  9. 文本文件必须是 LF（CRLF 会让 Linux 上的 .sh 直接执行失败）
 10. 全仓不得出现：真实主机名前缀形态、非保留网段的公网 IP、密钥形态、以及**任何**哈希
     命中禁止词表的词（含真实组织名、真实主机名、真实 IP）
"""
import hashlib
import os
import re

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")

# ---------------------------------------------------------------- 敏感信息守卫
# 真实名称**不以明文写在这里**，只留 sha256(lowercase)[:16]。
# 需要新增禁止词时执行：
#   python -c "import hashlib;print(hashlib.sha256(b'要禁止的词'.lower().encode()).hexdigest()[:16])"
# 然后把结果填进下面的集合（明文只存在于你的命令行历史里，不进仓库）。
FORBIDDEN_WORD_HASHES = frozenset({
    "c8b63871fda7faf7",   # 组织名（供应商）
    "527700c346738791",   # 组织名（集团）
    "afebc59b5d621bc2",   # 运营商名
    "08c09adf9a43224b",   # 真实主机名
    "8d69ff42dd8b6452",   # 真实主机名（带 WAN 后缀）
    "39108558830c6a35",   # 真实主机名
    "973c5e68ffec540d",   # 真实主机名
    "c5027ee311b1bbfc",   # 真实主机名
    "f2b926a811225ccb",   # 真实公网 IP
    "b7a8081ae8fb25c8",   # 生产内网 IP
    "a63fb198f74d34a4",   # 测试机公网 IP
})

# 真实站点命名前缀（形态匹配，能覆盖未知后缀的新主机名）
FORBIDDEN_HOSTNAME = re.compile(r"\b(?:AIC|HBF)_[A-Za-z0-9_]+\b")

# 密钥形态（与 deploy/package.py 的打包守卫一致）
SECRET_SHAPES = (
    (re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}\.[A-Za-z0-9]{16}(?![0-9A-Za-z])"), "疑似 LLM API Key"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "疑似 OpenAI 风格 Key"),
    (re.compile(r"access_token=(?!\$\{)[0-9a-fA-F]{24,}"), "疑似钉钉 access_token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥内容"),
    (re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])"), "疑似手机号"),
)

# 允许出现的 IPv4 前缀：回环 / 私网 / RFC 5737 文档保留 / 通配
ALLOWED_IP_PREFIXES = (
    "127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.2", "172.30.", "172.31.", "192.0.2.", "198.51.100.", "203.0.113.",
    "0.0.0.0", "255.255.255.255",
)
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")

# 扫描时跳过的目录
SKIP_DIRS = {".git", ".venv", "venv", "dist", "__pycache__", ".pytest_cache", "node_modules"}
TEXT_SUFFIXES = (".py", ".sh", ".sql", ".md", ".js", ".html", ".yaml", ".yml",
                 ".example", ".service", ".conf", ".txt", ".gitattributes",
                 ".gitignore", ".cfg", ".ini", ".json")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _iter_text_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(TEXT_SUFFIXES) or name in (".gitignore", ".gitattributes"):
                yield os.path.join(base, name)


def _candidates(text):
    """所有可能承载标识的字符串：词元 + IPv4。"""
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_.\-]{2,}", text):
        yield m.group(0)
    for m in IPV4.finditer(text):
        yield m.group(0)


@pytest.fixture(scope="module")
def readme():
    return _read(README)


def _registered_tool_names():
    from app.tools import build_tool_schemas

    return {t["name"] for t in build_tool_schemas()}


def _scan_repo():
    """返回 [(相对路径, 原因, 命中片段)]，覆盖全部文本文件。"""
    problems = []
    for path in _iter_text_files():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError):
            continue
        for m in FORBIDDEN_HOSTNAME.finditer(text):
            problems.append((rel, "真实主机名形态", m.group(0)))
        for rx, label in SECRET_SHAPES:
            for m in rx.finditer(text):
                problems.append((rel, label, m.group(0)[:16] + "…"))
        for ip in IPV4.finditer(text):
            value = ip.group(0)
            if not value.startswith(ALLOWED_IP_PREFIXES):
                problems.append((rel, "非保留网段的 IPv4", value))
        for token in _candidates(text):
            digest = hashlib.sha256(token.lower().encode()).hexdigest()[:16]
            if digest in FORBIDDEN_WORD_HASHES:
                problems.append((rel, "命中禁止词表（哈希）", token[:20] + "…"))
    return problems


# ---------------------------------------------------------------- 1 / 2 工具

def test_readme_lists_exactly_the_registered_tools(readme):
    registered = _registered_tool_names()
    assert registered, "注册表为空，说明 build_tool_schemas() 行为变了"

    mentioned = set(re.findall(r"`([a-z]+_[a-z_]+)`", readme))
    tool_like = {n for n in mentioned if n.startswith(("query_", "trend_", "send_"))}
    unknown = tool_like - registered - {"send_dingtalk"}
    assert not unknown, "README 提到未注册的工具名：%s" % sorted(unknown)

    missing = registered - mentioned
    assert not missing, "README 未提到已注册的工具：%s" % sorted(missing)


def test_send_dingtalk_is_not_exposed_to_llm(readme):
    assert "send_dingtalk" not in _registered_tool_names(), (
        "send_dingtalk 不得注册给 LLM：发送消息有真实外部副作用，"
        "交给模型自主决定会出现「没发却说发了」"
    )
    assert "没有**发送工具" in readme or "没有发送工具" in readme, (
        "README 必须写明「AI 没有发送工具」，否则用户会以为能用自然语言触发发送"
    )


# ------------------------------------------------------------------ 3 配置项

def _flatten(node, prefix=""):
    """把 config.yaml 拍平成点分叶子路径；suppliers 子树整体排除（README 用示例说明）。"""
    out = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = "%s.%s" % (prefix, key) if prefix else str(key)
            if path == "suppliers":
                continue
            if isinstance(value, dict):
                out |= _flatten(value, path)
            else:
                out.add(path)
    return out


def test_every_config_key_is_documented(readme):
    cfg = yaml.safe_load(_read(os.path.join(ROOT, "config.yaml")))
    keys = _flatten(cfg)
    assert len(keys) >= 20, "config.yaml 叶子字段数异常：%d" % len(keys)
    undocumented = sorted(k for k in keys if k not in readme)
    assert not undocumented, "README 未记录这些 config.yaml 字段：%s" % undocumented


# ------------------------------------------------------------ 4 环境变量

def test_every_env_var_is_documented(readme):
    env_text = _read(os.path.join(ROOT, ".env.example"))
    names = re.findall(r"^([A-Z][A-Z0-9_]*)=", env_text, re.M)
    assert names, ".env.example 里没解析到变量"
    undocumented = sorted(n for n in names if n not in readme)
    assert not undocumented, "README 未记录这些 .env 变量：%s" % undocumented


def test_env_example_has_no_real_value():
    """随仓库发布的模板里，每个 KEY= 的值必须是空或占位符。"""
    text = _read(os.path.join(ROOT, ".env.example"))
    bad = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value and not re.fullmatch(r"(CHANGE_ME\w*|<[^>]*>|\$\{[^}]*\})", value):
            bad.append("%s=%s…" % (key.strip(), value[:8]))
    assert not bad, ".env.example 的值必须是占位符或空：%s" % bad


# ---------------------------------------------------------------- 5 时段键

def test_every_period_key_is_documented(readme):
    from app.tools import PERIODS

    undocumented = sorted(k for k in PERIODS if k not in readme)
    assert not undocumented, "README 未记录这些时段键：%s" % undocumented


# ------------------------------------------------------- 6 目录结构完整性

def test_directory_tree_covers_repo_files(readme):
    """README「目录结构」一节必须给每个源文件/文档留位置。"""
    expected = set()
    for sub in ("app", "frontend", "deploy", "tests", "tests/js", "tests/integration"):
        d = os.path.join(ROOT, sub.replace("/", os.sep))
        if os.path.isdir(d):
            for name in os.listdir(d):
                if os.path.isfile(os.path.join(d, name)):
                    expected.add(name)
    for name in os.listdir(ROOT):
        path = os.path.join(ROOT, name)
        if os.path.isfile(path) and (name.endswith(".md") or name in (
                "config.yaml", ".env.example", "requirements.txt",
                "requirements-dev.txt", "conftest.py", ".gitignore", ".gitattributes",
                "LICENSE")):
            expected.add(name)

    missing = sorted(n for n in expected if n not in readme)
    assert not missing, "README 目录结构一节缺少这些文件：%s" % missing


# --------------------------------------------------------------- 7 版本号

def test_readme_version_matches_latest_tag(readme):
    try:
        import subprocess

        tags = subprocess.run(
            ["git", "-C", ROOT, "tag", "--list", "v*", "--sort=-v:refname"],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        pytest.skip("无 git 可用")
    if not tags:
        pytest.skip("仓库里没有 v* 标签")

    match = re.search(r"当前对应版本[：:]\s*\**\s*(v[0-9][0-9.]*)", readme)
    assert match, "README 顶部必须标注「当前对应版本：vX.Y.Z」"
    assert match.group(1) == tags[0], (
        "README 标注的版本 %s 与最新标签 %s 不一致" % (match.group(1), tags[0])
    )


# ------------------------------------------------------------------ 8 锚点

def _slug(heading):
    """按 GitHub 的锚点规则把标题转成 slug（保留中文，空格转连字符，去掉标点）。"""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\u4e00-\u9fff \-]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


@pytest.mark.parametrize("doc", ["README.md", "README.en.md", "DEPLOYMENT.md", "SECURITY.md",
                                 "RECOVERY-IDC.md", "CHANGELOG.md", "AGENTS.md",
                                 "tests/integration/README.md"])
def test_internal_anchor_links_resolve(doc):
    """同文件内的 (#锚点) 链接必须有对应标题——改标题时最容易忘记同步目录。"""
    path = os.path.join(ROOT, doc.replace("/", os.sep))
    if not os.path.isfile(path):
        pytest.skip("%s 不存在" % doc)
    text = _read(path)

    anchors = set()
    in_code = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            anchors.add(_slug(m.group(1)))

    links = re.findall(r"\]\(#([^)]+)\)", text)
    dangling = sorted({a for a in links if a not in anchors})
    assert not dangling, "%s 里有断掉的锚点链接：#%s" % (doc, " #".join(dangling))


# ------------------------------------------------------- 9 行尾 & 10 敏感信息

def test_text_files_use_lf():
    """CRLF 会让 Linux 上的 .sh 报 "bad interpreter"；Windows 开发机必须钉死 LF。"""
    attrs = os.path.join(ROOT, ".gitattributes")
    assert os.path.isfile(attrs), "缺少 .gitattributes（无法阻止 Windows 检出成 CRLF）"
    assert "eol=lf" in _read(attrs), ".gitattributes 必须包含 eol=lf"

    bad = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not (name.endswith(TEXT_SUFFIXES) or name == 'LICENSE'):
                continue
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                if b"\r" in fh.read():
                    bad.append(os.path.relpath(path, ROOT))
    assert not bad, "这些文件含 CR（应为 LF）：%s" % bad


def test_repo_contains_no_real_assets_or_secrets():
    """全仓扫描：真实主机名形态、非保留网段 IP、密钥形态、禁止词哈希。

    守卫本身不含任何真实名称（只有词哈希与形态），因此本测试通过即代表仓库是干净的。
    """
    problems = _scan_repo()
    assert not problems, "仓库中出现疑似真实资产/凭据：\n" + "\n".join(
        "  %s: %s -> %s" % (rel, why, hit) for rel, why, hit in problems[:20]
    )
