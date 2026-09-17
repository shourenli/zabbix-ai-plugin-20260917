"""离线包产物回归测试。

真实事故：在 Windows 上执行 `deploy/package.py`，生成的 `.sha256` 是 CRLF 行尾，
传到 Linux 目标机后 `sha256sum -c` 报 `No such file or directory` —— 也就是说
DEPLOYMENT.md 第 5.0 节要求的完整性校验步骤**根本跑不通**。
根因是 `Path.write_text()` 在 Windows 上默认做换行翻译。
"""
from __future__ import annotations

import hashlib
import re
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_PY = REPO / "deploy" / "package.py"
DIST = REPO / "dist"


def test_package_script_pins_lf_newline():
    """打包脚本必须显式指定 newline='\\n'，否则 Windows 上会写出 CRLF。"""
    src = PACKAGE_PY.read_text(encoding="utf-8")
    assert 'newline="\\n"' in src, (
        "deploy/package.py 写 .sha256 时必须显式指定 newline='\\n'，"
        "否则在 Windows 打包会产生 CRLF，目标机 sha256sum -c 必然失败"
    )


def _sidecars():
    return sorted(DIST.glob("*.tar.gz.sha256")) if DIST.is_dir() else []


@pytest.mark.parametrize("sum_path", _sidecars(), ids=lambda p: p.name)
def test_sha256_sidecar_is_lf_only(sum_path):
    """校验文件不能含 CR，否则 Linux 上 sha256sum -c 读不到目标文件。"""
    raw = sum_path.read_bytes()
    assert b"\r" not in raw, "%s 含 CR（CRLF 行尾），Linux 上 sha256sum -c 会失败" % sum_path.name
    assert raw.endswith(b"\n"), "%s 应以换行结尾" % sum_path.name


@pytest.mark.parametrize("sum_path", _sidecars(), ids=lambda p: p.name)
def test_sha256_sidecar_matches_tarball(sum_path):
    """校验文件里的摘要必须与同目录 tar.gz 实际摘要一致。"""
    tar_path = sum_path.with_suffix("")  # x.tar.gz.sha256 -> x.tar.gz
    assert tar_path.is_file(), "缺少对应的 %s" % tar_path.name
    declared = sum_path.read_text(encoding="utf-8").split()[0]
    actual = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    assert declared == actual, "%s 摘要与 %s 不符" % (sum_path.name, tar_path.name)


@pytest.mark.parametrize("sum_path", _sidecars(), ids=lambda p: p.name)
def test_sha256_sidecar_names_match_tarball(sum_path):
    """校验文件里记录的文件名必须与旁边的 tar.gz 同名（否则 -c 找不到文件）。"""
    tar_path = sum_path.with_suffix("")
    body = sum_path.read_text(encoding="utf-8").strip()
    assert body.endswith(tar_path.name), (
        "%s 记录的文件名不是 %s" % (sum_path.name, tar_path.name)
    )


# ------------------------------------------------------------ 内容级密钥守卫
# 真实事故：v1.0.0–v1.0.9 的 .env.example 里含一把真实 GLM API Key，被打进了 9 个离线包。
# 文件名黑名单挡不住这种事故，所以打包前增加了内容扫描；这里锁住它的行为。

def _pkg_module():
    """导入 deploy/package.py（deploy 目录无 __init__.py，按路径加载）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pkg_under_test", PACKAGE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _guard():
    return _pkg_module().assert_no_secrets


def test_secret_guard_rejects_llm_key_shape(tmp_path):
    """<32 位 hex>.<16 位> 形态的 Key 必须被拦下。"""
    leak = tmp_path / "leak.md"
    leak.write_text("LLM_API_KEY=" + "0" * 32 + "." + "A" * 16, encoding="utf-8")
    with pytest.raises(SystemExit):
        _guard()([leak])


def test_secret_guard_rejects_token_and_private_key(tmp_path):
    token = tmp_path / "token.md"
    token.write_text(
        "https://oapi.dingtalk.com/robot/send?access_token=" + "a" * 32,
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        _guard()([token])

    key = tmp_path / "id_rsa.txt"
    # 故意拼接构造：让仓库文件本身不含私钥头文本，避免被扫描器误判为凭据
    key.write_text("-----BEGIN " + "RSA PRIVATE KEY-----\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        _guard()([key])


def test_secret_guard_allows_placeholders(tmp_path):
    env = tmp_path / ".env.example"
    env.write_text("LLM_API_KEY=\nDINGTALK_NETWORK_A=你的token\n", encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        'webhook: "https://oapi.dingtalk.com/robot/send?access_token=${DINGTALK_NETWORK_A}"\n',
        encoding="utf-8",
    )
    _guard()([env, cfg])  # 占位符与 ${VAR} 引用不得误报


def test_env_example_is_generated_not_copied():
    """审计 C-1 的结构性修复：包内 .env.example 现场生成，不从仓库复制。"""
    mod = _pkg_module()
    assert ".env.example" not in mod.INCLUDE_FILES, (
        "仓库文件不得直接进包：包内模板必须由 ENV_TEMPLATE 现场生成"
    )
    assert mod.ENV_TEMPLATE_NAME == ".env.example"
    values = [line.split("=", 1)[1] for line in mod.ENV_TEMPLATE.splitlines()
              if "=" in line and not line.strip().startswith("#")]
    assert values, "生成的模板里应有 KEY= 行"
    assert all(v == "" for v in values), "生成的模板所有值必须为空"


def test_shipped_env_example_has_no_real_key():
    """随仓库发布的 .env.example 的 LLM_API_KEY 必须是空或占位符。"""
    env = REPO / ".env.example"
    values = [
        line.split("=", 1)[1].strip().strip('"').strip("'")
        for line in env.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("LLM_API_KEY=")
    ]
    assert values, ".env.example 里应有 LLM_API_KEY 行"
    assert all(v in ("", "CHANGE_ME", "your-api-key-here") for v in values), (
        ".env.example 的 LLM_API_KEY 必须是占位符（曾被真实 Key 泄露过）"
    )


# --------------------------------------------------------------- 文件收集

def _collector():
    return _pkg_module()._iter_payload_files


def test_collector_skips_pycache_and_cache_dirs(tmp_path):
    """真实事故：开发机跑过 pytest 后 `app/__pycache__` 存在，打包直接中止。"""
    (tmp_path / "app" / "__pycache__").mkdir(parents=True)
    (tmp_path / "app" / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"x")
    (tmp_path / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "CACHEDIR.TAG").write_text("x", encoding="utf-8")

    got = {p.name for p in _collector()(tmp_path)}
    assert got == {"main.py"}, "收集器应跳过 __pycache__/.pytest_cache，实际收到 %s" % got


def test_shipped_doc_matches_package_file_count():
    """DEPLOYMENT.md 里写的包内文件数必须与实际白名单一致（防止再次漂移）。"""
    mod = _pkg_module()
    # +1 = 打包时现场生成的 .env.example
    count = len(mod.collect_files(False)) + 1
    docs = (REPO / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert re.search(r"其中 %d 个文件" % count, docs), (
        "DEPLOYMENT.md 的「生产环境只需要其中 N 个文件」与实际不符（实际 %d 个）" % count
    )
    assert re.search(r"约 100 KB，%d 个文件" % count, docs), (
        "DEPLOYMENT.md 的「约 100 KB，N 个文件」与实际不符（实际 %d 个）" % count
    )
