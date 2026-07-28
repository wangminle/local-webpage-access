"""打包脚本回归测试（BUG-206 / BUG-286）。

BUG-206：`scripts/pack-release-zip.sh` 在 `set -o pipefail` 下原用
``unzip -l … | head -30``，head 关闭管道触发 SIGPIPE→unzip 退出 141，使脚本整体
返回 141、被发布流水线误判失败。修复后改为先把完整列表重定向到临时文件再 head。

BUG-286：脚本原用 ``rg``（ripgrep）做校验，在没装 ripgrep 的干净环境里直接失败。
旧测试注入 ``rg``→``grep`` 垫片，恰好把这个缺陷遮住了。现改用 POSIX ``grep``，
并用「最小 PATH」跑一遍锁死：脚本不得再隐式依赖非标准工具。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "pack-release-zip.sh"


def _have_tools() -> bool:
    return all(shutil.which(t) for t in ("bash", "unzip", "zip"))


def _run(out: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), str(out)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.mark.skipif(not _have_tools(), reason="缺少 bash/unzip/zip 之一")
def test_pack_release_zip_exits_zero(tmp_path: Path) -> None:
    """BUG-206：脚本退出码必须为 0（不能因 SIGPIPE 误报 141）。"""
    out = tmp_path / "lwa-src.zip"
    result = _run(out, dict(os.environ))
    assert result.returncode == 0, (
        f"pack-release-zip.sh 退出码 {result.returncode}（期望 0）\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert out.is_file()
    assert out.stat().st_size > 0


@pytest.mark.skipif(not _have_tools(), reason="缺少 bash/unzip/zip 之一")
def test_pack_release_zip_works_without_ripgrep(tmp_path: Path) -> None:
    """BUG-286：干净环境（PATH 仅 /usr/bin:/bin，无 rg）也必须打包成功。"""
    out = tmp_path / "lwa-src-min-path.zip"
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"
    result = _run(out, env)
    assert result.returncode == 0, (
        "最小 PATH 下打包失败，脚本可能又引入了非标准工具依赖\n"
        f"退出码 {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert out.is_file()
    assert out.stat().st_size > 0
