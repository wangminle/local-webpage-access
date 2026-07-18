"""打包脚本回归测试（BUG-206）。

`scripts/pack-release-zip.sh` 在 `set -o pipefail` 下原用 ``unzip -l … | head -30``，
head 关闭管道触发 SIGPIPE→unzip 退出 141，使脚本整体返回 141、被发布流水线误判失败。
修复后改为先把完整列表重定向到临时文件（unzip 正常写完、无管道）再 head/rg。

本测试直接运行脚本并断言退出码为 0。脚本依赖 ripgrep（``rg``）做校验——开发者环境
通常已装；本机若无真实 ``rg``（如仅为 shell 函数），则注入一个最小 ``rg``→``grep``
垫片到子进程 PATH，使脚本能完整跑完，从而稳定验证退出码（校验的是 BUG-206 的
SIGPIPE 修复，而非 rg 本身）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "pack-release-zip.sh"

# 最小 rg→grep 垫片：覆盖脚本用到的 `rg -q PAT FILE` 与 `rg -n PAT FILE`。
_RG_SHIM = """\
#!/usr/bin/env bash
# 仅满足 pack-release-zip.sh 的 rg 调用（-q 静默匹配 / -n 打印匹配行）。
set -euo pipefail
quiet=0; lineno=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -q) quiet=1; shift;;
    -n) lineno=1; shift;;
    --) shift; break;;
    -*) shift;;
    *) break;;
  esac
done
pattern="$1"; shift
files=("$@")
if (( quiet )); then
  grep -qE "$pattern" "${files[@]}"
else
  grep -nE "$pattern" "${files[@]}" || true
fi
"""


def _have_tools() -> bool:
    return all(shutil.which(t) for t in ("bash", "unzip", "zip"))


@pytest.mark.skipif(not _have_tools(), reason="缺少 bash/unzip/zip 之一")
def test_pack_release_zip_exits_zero(tmp_path: Path) -> None:
    """BUG-206：脚本退出码必须为 0（不能因 SIGPIPE 误报 141）。"""
    out = tmp_path / "lwa-src.zip"
    env = dict(os.environ)
    # 无真实 rg 时注入垫片，使脚本完整执行以验证退出码
    if not shutil.which("rg"):
        bin_dir = tmp_path / "shim-bin"
        bin_dir.mkdir()
        shim = bin_dir / "rg"
        shim.write_text(_RG_SHIM)
        shim.chmod(0o755)
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    result = subprocess.run(
        ["bash", str(_SCRIPT), str(out)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"pack-release-zip.sh 退出码 {result.returncode}（期望 0）\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert out.is_file()
    assert out.stat().st_size > 0
