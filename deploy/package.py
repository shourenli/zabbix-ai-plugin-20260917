#!/usr/bin/env python3
"""生成 Zabbix AI 插件【生产部署离线包】。

在【开发机】执行（Windows / Linux 均可，仅用标准库）：

    python deploy/package.py                    # 输出到 dist/
    python deploy/package.py --out /tmp/dist    # 指定输出目录
    python deploy/package.py --with-tests       # 额外打入测试，便于目标机上自测

产物：

    dist/zabbix-ai-plugin-<version>.tar.gz
    dist/zabbix-ai-plugin-<version>.tar.gz.sha256

设计要点：

1. **白名单收集**：只按下面的 INCLUDE_* 清单取文件，因此 `.env`、`.venv`、
   `__pycache__`、`.git` 等**在机制上就不可能**被打进包，避免密钥随包外泄。
   打包前还会做一次"禁止内容"断言，双重保险。
2. **包内自带 MANIFEST.txt**：版本、git commit、构建时间、每个文件的大小与 SHA256，
   便于核对来源与完整性。
3. 解压后顶层目录为 `zabbix-ai-plugin-<version>/`，不会污染目标机当前目录。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---- 白名单：只有这些会被打进包 ----
INCLUDE_DIRS = ["app", "frontend", "deploy"]
INCLUDE_FILES = [
    "config.yaml",
    "requirements.txt",
    "LICENSE",
    "README.md",
    "README.en.md",
    "DEPLOYMENT.md",
    "RECOVERY-IDC.md",
]
OPTIONAL_TESTS = ["conftest.py", "requirements-dev.txt", "tests"]

# ---- 黑名单：任何情况下都不允许出现在包里 ----
FORBIDDEN_NAMES = {".env", ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".pyd", ".db", ".sqlite", ".log", ".swp")

# ---- 内容级密钥守卫 ----
# 文件名黑名单挡不住"真实密钥被写在 .env.example 里"这类事故：v1.0.0–v1.0.9 的
# .env.example 就曾含一把真实 GLM Key，并被打进 9 个离线包（见 SECURITY.md）。
# 因此打包前再把每个文本文件扫一遍，命中就中止。
TEXT_SUFFIXES = (".py", ".md", ".yaml", ".yml", ".txt", ".example", ".sh", ".sql",
                 ".js", ".html", ".service", ".conf", ".cfg", ".ini", ".json")
SECRET_PATTERNS = (
    (re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}\.[A-Za-z0-9]{16}(?![0-9A-Za-z])"),
     "疑似 LLM API Key（<32 位 hex>.<16 位>）"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "疑似 OpenAI 风格 API Key"),
    (re.compile(r"access_token=(?!\$\{)[0-9a-fA-F]{24,}"), "疑似钉钉机器人 access_token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥内容"),
)
ENV_PLACEHOLDERS = {"", "CHANGE_ME", "your-api-key-here", "xxx", "你的key"}

# ---- 包内 .env 模板：打包时现场生成，不复制仓库文件 ----
# 第三方审计（C-1）：`.env.example` 曾被真实密钥污染并随包分发。即使仓库文件已清空，
# 「复制仓库文件」这条链路仍可能在某次失误中把密钥带出去，因此包内模板改为常量生成。
ENV_TEMPLATE_NAME = ".env.example"
ENV_TEMPLATE = """\
# 由 deploy/package.py 在打包时生成：所有值均为空，请在目标机填写后另存为 .env
# 本文件是模板，不要在这里写真实密钥。
LLM_API_KEY=
MYSQL_PASSWORD=
ZABBIX_SERVICE_USER=
ZABBIX_SERVICE_PASSWORD=
DINGTALK_NETWORK_A=
DINGTALK_IDC_B=
DINGTALK_NETWORK_A_SECRET=
DINGTALK_IDC_B_SECRET=
"""


def assert_env_template_clean(data: bytes) -> None:
    """生成的模板里每个 KEY= 必须是空值/占位符（常量也要守，防止将来被改坏）。"""
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value not in ENV_PLACEHOLDERS:
            raise SystemExit(
                f"[中止] 生成式 .env 模板的 {key.strip()} 含非占位符值：{value[:8]}…"
            )


def assert_no_secrets(files: list[Path]) -> None:
    """打包前的内容级检查：包里不允许出现任何真实密钥。"""
    for p in files:
        if not p.name.endswith(TEXT_SUFFIXES):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for rx, what in SECRET_PATTERNS:
            m = rx.search(text)
            if m:
                try:
                    where = p.relative_to(ROOT)
                except ValueError:      # 传入的不是仓库内文件时也要能正确报错
                    where = p
                raise SystemExit(
                    f"[中止] {where} 里发现{what}：{m.group(0)[:10]}…\n"
                    "        包内不得含真实密钥，请先清空为占位符再打包。"
                )

    env = ROOT / ".env.example"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("LLM_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value not in ENV_PLACEHOLDERS:
                    raise SystemExit(
                        "[中止] .env.example 的 LLM_API_KEY 不是占位符"
                        f"（当前值以 {value[:6]}… 开头）。"
                    )



def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=15
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def detect_version() -> str:
    """优先取 git tag；否则退回 commit 短号；再否则用 dev。"""
    describe = _run(["git", "describe", "--tags", "--always"])
    if describe:
        # v1.0.0-3-gabc1234 -> v1.0.0-3-gabc1234（保留，能体现"领先 tag 几个提交"）
        return describe.replace("/", "-")
    return "dev"


def detect_commit() -> str:
    return _run(["git", "rev-parse", "HEAD"]) or "(非 git 工作区)"


def detect_branch() -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]) or "-"


def _iter_payload_files(base: Path):
    """遍历目录时**直接跳过**黑名单目录/后缀。

    真实事故：`collect_files` 原先用 `rglob('*')` 收文件，只要开发机上跑过一次 pytest
    （`app/__pycache__` 存在），收集结果里就会带进 `.pyc`，随后被"禁止内容校验"中止——
    即"跑过测试就再也打不出包"。文档承诺的是"机制上不可能进包"，这里按承诺修掉。
    """
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        if any(part in FORBIDDEN_NAMES for part in p.relative_to(base).parts):
            continue
        if p.name.endswith(FORBIDDEN_SUFFIXES):
            continue
        yield p


def collect_files(with_tests: bool) -> list[Path]:
    """按白名单收集文件，并做禁止内容校验。"""
    files: list[Path] = []

    for name in INCLUDE_FILES:
        p = ROOT / name
        if p.is_file():
            files.append(p)
        else:
            print(f"  [warn] 缺少文件：{name}")

    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.is_dir():
            print(f"  [warn] 缺少目录：{d}")
            continue
        files.extend(_iter_payload_files(base))

    if with_tests:
        for name in OPTIONAL_TESTS:
            p = ROOT / name
            if p.is_dir():
                files.extend(_iter_payload_files(p))
            elif p.is_file():
                files.append(p)

    # 去重 + 排序，保证可复现
    files = sorted(set(files), key=lambda x: str(x.relative_to(ROOT)).lower())

    # 禁止内容校验（白名单下本不该出现，作为双保险）
    for p in files:
        rel = p.relative_to(ROOT)
        for part in rel.parts:
            if part in FORBIDDEN_NAMES:
                raise SystemExit(f"[中止] 包内不允许出现 {part}：{rel}")
        if rel.name.endswith(FORBIDDEN_SUFFIXES):
            raise SystemExit(f"[中止] 包内不允许出现该类型文件：{rel}")
    return files


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(version: str, files: list[Path], with_tests: bool,
                   extra: list[tuple[str, bytes]] | None = None) -> str:
    # (相对路径, sha256, 大小)：仓库文件 + 现场生成的条目
    entries: list[tuple[str, str, int]] = [
        (p.relative_to(ROOT).as_posix(), sha256_of(p), p.stat().st_size) for p in files
    ]
    for rel, data in (extra or []):
        entries.append((rel, hashlib.sha256(data).hexdigest(), len(data)))
    entries.sort(key=lambda e: e[0].lower())

    lines = [
        "Zabbix AI 对话框插件（zabbix-ai-plugin）—— 生产部署包",
        "=" * 66,
        f"版本        : {version}",
        f"git commit  : {detect_commit()}",
        f"git branch  : {detect_branch()}",
        f"构建时间(UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}",
        f"包含测试    : {'是' if with_tests else '否'}",
        f"文件数量    : {len(entries)}",
        "",
        "部署步骤见包内 DEPLOYMENT.md；部署后请执行 deploy/verify.sh 自检。",
        "包内不含任何密钥：.env 需在目标机上由 .env.example 生成并填写。",
        "",
        "文件清单（相对包根目录）：",
        "-" * 66,
    ]
    for rel, digest, size in entries:
        lines.append(f"{digest}  {size:>8}  {rel}")
    lines.append("-" * 66)
    return "\n".join(lines) + "\n"


def build(out_dir: Path, with_tests: bool) -> int:
    version = detect_version()
    top = f"zabbix-ai-plugin-{version}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tar_path = out_dir / f"{top}.tar.gz"

    print(f"源目录 : {ROOT}")
    print(f"版本   : {version}")
    files = collect_files(with_tests)
    if not files:
        raise SystemExit("[中止] 没有收集到任何文件")
    assert_no_secrets(files)
    print(f"文件数 : {len(files) + 1}（含测试：{'是' if with_tests else '否'}；"
          f"其中 {ENV_TEMPLATE_NAME} 为现场生成）")

    env_bytes = ENV_TEMPLATE.encode("utf-8")
    assert_env_template_clean(env_bytes)
    manifest = build_manifest(
        version, files, with_tests, extra=[(ENV_TEMPLATE_NAME, env_bytes)]
    ).encode("utf-8")

    with tarfile.open(tar_path, "w:gz") as tar:
        # 先放 MANIFEST.txt
        import io

        info = tarfile.TarInfo(f"{top}/MANIFEST.txt")
        info.size = len(manifest)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(info, io.BytesIO(manifest))

        for p in files:
            rel = p.relative_to(ROOT).as_posix()
            tar.add(str(p), arcname=f"{top}/{rel}")

        # 包内 .env 模板：现场生成（不复制仓库文件），内容恒定、所有值为空
        env_info = tarfile.TarInfo(f"{top}/{ENV_TEMPLATE_NAME}")
        env_info.size = len(env_bytes)
        env_info.mode = 0o644
        env_info.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(env_info, io.BytesIO(env_bytes))

    digest = sha256_of(tar_path)
    sum_path = tar_path.with_suffix(tar_path.suffix + ".sha256")
    # newline="\n" 必须显式指定：Windows 上 write_text 默认会把 \n 翻成 CRLF，
    # 而目标机是 Linux，sha256sum -c 会因行尾的 \r 报
    # "No such file or directory" 导致校验步骤直接失败。
    sum_path.write_text(
        f"{digest}  {tar_path.name}\n", encoding="utf-8", newline="\n"
    )

    size_kb = tar_path.stat().st_size / 1024
    print()
    print("=" * 66)
    print(f"已生成 : {tar_path}  ({size_kb:.1f} KB)")
    print(f"SHA256 : {digest}")
    print(f"校验文件: {sum_path}")
    print("=" * 66)
    print()
    print("目标机部署：")
    print(f"  tar -xzf {tar_path.name}")
    print(f"  cd {top}")
    print("  # 然后按 DEPLOYMENT.md 第 5 节执行")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="生成生产部署离线包")
    ap.add_argument("--out", default="dist", help="输出目录（默认 dist/）")
    ap.add_argument("--with-tests", action="store_true", help="额外打入测试文件")
    args = ap.parse_args()

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    return build(out, args.with_tests)


if __name__ == "__main__":
    sys.exit(main())
