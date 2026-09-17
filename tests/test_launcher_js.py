"""前端脚本的行为自测入口（node + 最小 DOM 桩，真执行线上那份脚本）。

- `tests/js/launcher.test.js`：漂浮球（frontend/launcher.js）
- `tests/js/suggest.test.js` ：`/供应商名` 建议列表的键盘操作（frontend/index.html 内联脚本）

没装 node 的机器自动跳过，因此不会影响其它环境跑 `python -m pytest`。

为什么值得进常规测试：这两处都**在浏览器里点不到就发现不了**——
漂浮球刻意吞掉所有异常（出错只表现为"球没出来"），实测抓到过一次致命问题
（关闭按钮变量与关闭函数同名，严格模式下整段脚本加载失败）；
而建议列表若写错，表现是"打 `/` 没反应"或"按回车发不出去"。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parent.parent
JS_TESTS = ["launcher.test.js", "suggest.test.js"]


@pytest.mark.skipif(NODE is None, reason="未安装 node，跳过前端脚本自测")
@pytest.mark.parametrize("script", JS_TESTS)
def test_frontend_js_behaviour(script):
    path = ROOT / "tests" / "js" / script
    result = subprocess.run(
        [NODE, str(path)],
        capture_output=True,
        text=True,
        # 必须显式指定 UTF-8：node 输出的是 UTF-8 中文，交给系统默认编码
        # （Windows 上常是 GBK）解码会抛 UnicodeDecodeError，而且是在读取线程里
        # 抛出、只表现为 stdout 变成 None——排查起来很绕。
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    assert result.returncode == 0, "%s 行为自测失败：\n%s\n%s" % (
        script, result.stdout, result.stderr,
    )
    assert "0 项失败" in result.stdout
