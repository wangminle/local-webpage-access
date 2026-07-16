"""``lwa setup`` 宿主机环境检测与安装指引。

在 ``lwa init`` 之前即可运行：检测 Python / lwa 包 / Docker / Compose / Caddy / Node，
按当前操作系统输出安装说明，并可生成参考安装脚本（不自动执行）。
"""

from __future__ import annotations

import plistlib
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_webpage_access import PRODUCT_NAME, __version__
from local_webpage_access.config import Config
from local_webpage_access.doctor import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    SubprocessRunner,
    _default_runner,
    check_caddy,
    check_docker,
    check_docker_compose,
    check_python_packages,
    check_python_version,
)
from local_webpage_access.version_requirements import (
    MIN_CADDY_VERSION,
    MIN_COMPOSE_VERSION,
    MIN_DOCKER_VERSION,
    MIN_FASTAPI_VERSION,
    MIN_NODE_VERSION,
    MIN_UVICORN_VERSION,
    RECOMMENDED_COMPOSE_VERSION,
    version_ge,
)

# ---- 数据结构 ---------------------------------------------------------------


@dataclass
class SetupItem:
    """单项环境组件的检测结果与安装指引。"""

    name: str
    status: str  # ok / warn / fail / skip
    message: str
    required: str
    install_hint: str
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "required": self.required,
            "install_hint": self.install_hint,
            "optional": self.optional,
        }


@dataclass
class SetupReport:
    """完整环境搭建报告。"""

    platform: str
    items: list[SetupItem] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.has_failures

    @property
    def has_failures(self) -> bool:
        return any(i.status == STATUS_FAIL for i in self.items if not i.optional)


# ---- 平台识别 ---------------------------------------------------------------

# 平台识别集中在 :mod:`platform_detect`（IMP-030 增补 ``wsl``），此处再导出以保持
# 向后兼容——``tests/test_setup.py`` 仍按 ``setup.detect_platform`` 做 monkeypatch，
# 且模块内多处直接调用 ``detect_platform()``（解析为模块全局名，monkeypatch 生效）。
from local_webpage_access.platform_detect import detect_platform  # noqa: E402


def _hint_platform(plat: str | None = None) -> str:
    """安装指引用的平台：WSL 复用 Linux 指引。"""
    p = plat or detect_platform()
    return "linux" if p == "wsl" else p


# ---- 检测项 -----------------------------------------------------------------


def _check_lwa_package() -> SetupItem:
    return SetupItem(
        name="lwa",
        status=STATUS_OK,
        message=f"{PRODUCT_NAME} (lwa) {__version__} 已安装",
        required="pip install -e .",
        install_hint=(
            "在项目根目录执行：`pip install -e .`（开发）或 `pip install .`（安装）"
        ),
    )


def _check_node(runner: SubprocessRunner, plat: str | None = None) -> SetupItem:
    """Node.js 用于前端 SPA 构建；纯静态/容器后端可不装。"""
    node_hint_plat = _hint_platform(plat)
    if shutil.which("node") is None:
        return SetupItem(
            name="nodejs",
            status=STATUS_WARN,
            message="未检测到 node 命令",
            required=f"Node.js ≥ {MIN_NODE_VERSION}",
            install_hint=_node_install_hint(node_hint_plat),
            optional=True,
        )
    result = runner(["node", "--version"])
    version = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0 or not version_ge(version, MIN_NODE_VERSION):
        return SetupItem(
            name="nodejs",
            status=STATUS_WARN,
            message=f"Node.js {version or '?'} 低于推荐版本 ≥ {MIN_NODE_VERSION}",
            required=f"Node.js ≥ {MIN_NODE_VERSION}",
            install_hint=_node_install_hint(node_hint_plat),
            optional=True,
        )
    return SetupItem(
        name="nodejs",
        status=STATUS_OK,
        message=f"Node.js {version}（≥ {MIN_NODE_VERSION}）",
        required=f"Node.js ≥ {MIN_NODE_VERSION}",
        install_hint="已满足；前端 SPA 构建需要 npm/pnpm/yarn",
        optional=True,
    )


def _from_doctor_check(
    check,
    *,
    name: str,
    required: str,
    install_hint: str,
    optional: bool = False,
    **kwargs,
) -> SetupItem:
    result = check(**kwargs) if kwargs else check()
    return SetupItem(
        name=name,
        status=result.status,
        message=result.message,
        required=required,
        install_hint=result.suggestion or install_hint,
        optional=optional,
    )


def run_setup(
    *,
    static_gateway: str = "caddy",
    runner: SubprocessRunner = _default_runner,
) -> SetupReport:
    """检测宿主机环境并生成安装指引（不需要已初始化工作区）。"""
    plat = detect_platform()
    hint_plat = _hint_platform(plat)  # WSL 复用 Linux 安装指引
    from local_webpage_access.config import Config

    config = Config(staticGateway=static_gateway)
    items: list[SetupItem] = [
        _from_doctor_check(
            check_python_version,
            name="python",
            required="Python ≥ 3.13",
            install_hint=_python_install_hint(hint_plat),
        ),
        _check_lwa_package(),
        _from_doctor_check(
            check_python_packages,
            name="python_packages",
            required=(
                f"fastapi ≥ {MIN_FASTAPI_VERSION}，uvicorn ≥ {MIN_UVICORN_VERSION}"
            ),
            install_hint=(
                f"pip install -U 'fastapi>={MIN_FASTAPI_VERSION}' "
                f"'uvicorn>={MIN_UVICORN_VERSION}' 或 pip install -e ."
            ),
        ),
        _from_doctor_check(
            check_docker,
            name="docker",
            required=f"Docker ≥ {MIN_DOCKER_VERSION}",
            install_hint=_docker_install_hint(hint_plat),
            runner=runner,
        ),
        _from_doctor_check(
            check_docker_compose,
            name="docker_compose",
            required=(
                f"Docker Compose ≥ {MIN_COMPOSE_VERSION}"
                f"（推荐 ≥ {RECOMMENDED_COMPOSE_VERSION}）"
            ),
            install_hint=_compose_install_hint(hint_plat),
            runner=runner,
        ),
        _from_doctor_check(
            check_caddy,
            name="caddy",
            required=f"Caddy ≥ {MIN_CADDY_VERSION}（缺失时降级 builtin）",
            install_hint=_caddy_install_hint(hint_plat),
            optional=static_gateway != "caddy",
            config=config,
            runner=runner,
        ),
        _check_node(runner, hint_plat),
    ]
    return SetupReport(platform=plat, items=items)


# ---- 安装指引文案 -----------------------------------------------------------


def _python_install_hint(plat: str) -> str:
    if plat == "macos":
        return "推荐：`brew install python@3.13` 或从 https://www.python.org/downloads/ 安装"
    if plat == "linux":
        return "推荐：发行版包管理器安装 python3.13，或用 pyenv / uv 管理版本"
    if plat == "windows":
        return "推荐：从 https://www.python.org/downloads/ 安装 3.13+，并勾选 Add to PATH"
    return "安装 Python 3.13+ 并确保 `python3` / `pip` 在 PATH 中"


def _docker_install_hint(plat: str) -> str:
    if plat == "macos":
        return (
            "安装 Docker Desktop（含 Compose 插件）："
            "https://docs.docker.com/desktop/setup/install/mac-install/"
        )
    if plat == "linux":
        return (
            "按官方文档安装 Docker Engine + compose 插件："
            "https://docs.docker.com/engine/install/"
        )
    if plat == "windows":
        return (
            "安装 Docker Desktop for Windows："
            "https://docs.docker.com/desktop/setup/install/windows-install/"
        )
    return f"安装 Docker ≥ {MIN_DOCKER_VERSION} 并启动 dockerd"


def _compose_install_hint(plat: str) -> str:
    return (
        f"需要 Docker Compose 插件（`docker compose`），版本 ≥ {MIN_COMPOSE_VERSION}，"
        f"推荐 ≥ {RECOMMENDED_COMPOSE_VERSION}。"
        f"{_docker_install_hint(plat)}（Desktop 通常已捆绑；Linux 可 `apt install docker-compose-plugin`）"
    )


def _caddy_install_hint(plat: str) -> str:
    if plat == "macos":
        return f"推荐：`brew install caddy`（需 ≥ {MIN_CADDY_VERSION}）"
    if plat == "linux":
        return (
            f"官方 apt/yum 仓库：https://caddyserver.com/docs/install#debian-ubuntu-raspbian "
            f"（需 ≥ {MIN_CADDY_VERSION}）"
        )
    if plat == "windows":
        return (
            f"推荐：`winget install CaddyServer.Caddy` 或 "
            f"https://caddyserver.com/docs/install#windows "
            f"（需 ≥ {MIN_CADDY_VERSION}）"
        )
    return f"安装 Caddy ≥ {MIN_CADDY_VERSION}；或将 local-web.yml 的 staticGateway 设为 builtin"


def _node_install_hint(plat: str) -> str:
    if plat == "macos":
        return f"推荐：`brew install node@24` 或 fnm/nvm 安装 Node ≥ {MIN_NODE_VERSION}"
    if plat == "linux":
        return f"推荐：NodeSource / fnm / nvm 安装 Node ≥ {MIN_NODE_VERSION}"
    if plat == "windows":
        return f"推荐：`winget install OpenJS.NodeJS.LTS` 或 fnm 安装 Node ≥ {MIN_NODE_VERSION}"
    return f"安装 Node.js ≥ {MIN_NODE_VERSION}（仅前端 SPA 构建需要）"


# ---- 输出格式化 -------------------------------------------------------------


def format_setup_report(report: SetupReport) -> str:
    lines: list[str] = []
    lines.append(f"── 宿主机环境检测（{report.platform}）──")
    for item in report.items:
        tag = item.status.upper()
        opt = "（可选）" if item.optional else ""
        lines.append(f"  [{tag:4}] {item.name}{opt}: {item.message}")
        lines.append(f"           要求：{item.required}")
        if item.status != STATUS_OK:
            lines.append(f"           安装：{item.install_hint}")
    lines.append("")
    if report.ready:
        lines.append("下一步：")
        lines.append("  1. lwa init          # 初始化工作区")
        lines.append("  2. lwa doctor        # 复核环境（含端口池/registry，需工作区）")
        lines.append("  3. lwa import ...    # 导入 zip 并启动")
    else:
        lines.append("请先按上方「安装」指引补齐必需组件，然后执行：")
        lines.append("  lwa setup            # 重新检测")
        lines.append("  lwa setup --script   # 查看当前平台参考安装脚本")
    lines.append("")
    lines.append(
        "提示：`lwa setup` 检测宿主机工具；`lwa init` 初始化工作区；"
        "`lwa doctor` 在工作区就绪后做完整诊断。"
    )
    return "\n".join(lines)


# ---- 参考安装脚本 -----------------------------------------------------------


def render_setup_script(plat: str | None = None) -> str:
    """生成当前平台的参考安装脚本（注释为主，需人工审阅后执行）。"""
    plat = _hint_platform(plat)
    if plat == "macos":
        return _SCRIPT_MACOS
    if plat == "linux":
        return _SCRIPT_LINUX
    if plat == "windows":
        return _SCRIPT_WINDOWS
    return _SCRIPT_GENERIC


_SCRIPT_MACOS = """\
#!/usr/bin/env bash
# lwa 宿主机环境参考安装脚本（macOS）—— 请审阅后逐段执行，不保证覆盖所有环境。
set -euo pipefail

echo "==> Python 3.13+"
if ! command -v python3 &>/dev/null; then
  brew install python@3.13
fi

echo "==> 安装 lwa（在项目根目录执行）"
# pip install -e .

echo "==> Docker Desktop（含 Compose 插件，Docker 需 ≥ 29.0.0；Compose 最低 ≥ 2.40.2，推荐 ≥ 5.2.0）"
if ! command -v docker &>/dev/null; then
  brew install --cask docker
  echo "请启动 Docker Desktop 应用"
fi

echo "==> Caddy（推荐；缺失时 staticGateway=caddy 会降级 builtin，Caddy 模式需 ≥ 2.10.0）"
if ! command -v caddy &>/dev/null; then
  brew install caddy
fi

echo "==> Node.js（前端 SPA 构建需要，推荐 ≥ 24）"
if ! command -v node &>/dev/null; then
  brew install node@24
fi

echo "==> 验证"
python3 --version
docker version
docker compose version --short
caddy version || true
node --version || true
lwa setup
"""

_SCRIPT_LINUX = """\
#!/usr/bin/env bash
# lwa 宿主机环境参考安装脚本（Linux）—— 请审阅后逐段执行；需 root/sudo 权限。
set -euo pipefail

echo "==> Python 3.13+"
# 示例（Debian/Ubuntu）：sudo apt install python3.13 python3.13-venv python3-pip
# 或使用 pyenv / uv

echo "==> 安装 lwa（在项目根目录执行）"
# pip install -e .

echo "==> Docker Engine + Compose 插件"
# 官方文档：https://docs.docker.com/engine/install/
# 示例：sudo apt install docker-ce docker-ce-cli containerd.io docker-compose-plugin
# sudo usermod -aG docker "$USER" && newgrp docker

echo "==> Caddy（推荐；缺失时 staticGateway=caddy 会降级 builtin，Caddy 模式需 ≥ 2.10.0）"
# 官方文档：https://caddyserver.com/docs/install#debian-ubuntu-raspbian

echo "==> Node.js（前端 SPA 构建需要，推荐 ≥ 24）"
# 示例：fnm / nvm / NodeSource

echo "==> 验证"
python3 --version
docker version
docker compose version --short
caddy version || true
node --version || true
lwa setup
"""

_SCRIPT_WINDOWS = """\
# lwa 宿主机环境参考安装脚本（Windows PowerShell）—— 请审阅后逐段执行。
# 在 PowerShell（管理员）中运行。

Write-Host "==> Python 3.13+"
# winget install Python.Python.3.13
# 或从 https://www.python.org/downloads/ 安装并勾选 Add to PATH

Write-Host "==> 安装 lwa（在项目根目录执行）"
# pip install -e .

Write-Host "==> Docker Desktop（含 Compose，Docker 需 ≥ 29.0.0；Compose 最低 ≥ 2.40.2，推荐 ≥ 5.2.0）"
# winget install Docker.DockerDesktop
# 安装后启动 Docker Desktop

Write-Host "==> Caddy（推荐；缺失时 staticGateway=caddy 会降级 builtin，Caddy 模式需 ≥ 2.10.0）"
# winget install CaddyServer.Caddy

Write-Host "==> Node.js（前端 SPA 构建需要，推荐 ≥ 24）"
# winget install OpenJS.NodeJS.LTS

Write-Host "==> 验证"
python --version
docker version
docker compose version --short
caddy version
node --version
lwa setup
"""

_SCRIPT_GENERIC = """\
# lwa 宿主机环境参考安装脚本（通用）
# 请根据操作系统查阅：
#   lwa setup          # 检测并查看安装指引
#   docs/faq.md        # 排障文档
# 组件要求：Python 3.13+、Docker ≥ 29.0.0、Compose ≥ 2.40.2（推荐 ≥ 5.2.0）、Caddy ≥ 2.10.0（可选）、Node ≥ 24.0.0（前端构建）
"""


# ---- 开机自启（IMP-030：launchd 前台监管 plist 生成）------------------------

LAUNCHD_LABEL_PREFIX = "com.fenix.lwa"


def generate_launchd_plists(
    workspace_root: Path,
    config: Config,
    *,
    python_exe: str | None = None,
    include_caddy: bool = False,
    dest_dir: Path | None = None,
) -> list[tuple[str, Path]]:
    """生成 macOS launchd plist（**前台监管**，IMP-030）。

    返回 ``[(服务名, plist 路径)]``。非 macOS 抛错；dest_dir 默认
    ``~/Library/LaunchAgents/``。生成的 plist 用绝对 python 路径执行前台入口
    ``python -m local_webpage_access.<daemon|manager_service|gateway_service>
    --workspace <root>``，并固化 ``EnvironmentVariables.PATH``（含 Homebrew）+
    ``KeepAlive``——launchd 直接监管真实前台进程，崩溃即拉起（修复 BUG-138/139）。
    生成逻辑复用 :mod:`autostart`，避免两套实现（030.h）；与 ``lwa X off`` 的冲突
    由 ``lwa autostart disable`` / off 协调先 bootout 单元解决（030.b）。
    """
    from local_webpage_access.autostart import (
        build_launchd_plist,
        launchd_label,
        select_services,
    )
    from local_webpage_access.errors import LifecycleError

    if detect_platform() != "macos":
        raise LifecycleError(
            "launchd 开机自启仅支持 macOS；Linux/WSL 请用 `lwa autostart install`"
            "（systemd user service），Windows 请用任务计划程序（参考 docs/autostart.md）",
        )
    python = python_exe or sys.executable
    dest = dest_dir or (Path.home() / "Library" / "LaunchAgents")
    dest.mkdir(parents=True, exist_ok=True)
    # launchd 写 stdout/stderr 到 logs/，确保目录存在
    (workspace_root / "logs").mkdir(parents=True, exist_ok=True)

    written: list[tuple[str, Path]] = []
    for name in select_services(config, with_caddy=include_caddy):
        plist = build_launchd_plist(
            name, python_exe=python, workspace_root=workspace_root, keep_alive=True
        )
        path = dest / f"{launchd_label(name)}.plist"
        path.write_bytes(
            plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=False)
        )
        written.append((name, path))
    return written


def format_autostart_report(
    written: list[tuple[str, Path]],
    *,
    skipped_caddy: bool = False,
) -> str:
    """渲染开机自启报告（生成的 plist + 启用/完备性检查指引）。"""
    lines: list[str] = ["── 开机自启（macOS launchd，前台监管）──"]
    for name, path in written:
        lines.append(f"  · {name}: {path}")
    if skipped_caddy:
        lines.append("  （未生成 caddy 自启：staticGateway 非 caddy；如需请先切换后重跑）")
    lines.append("")
    lines.append("推荐用新命令完成启用 + 完备性检查（IMP-030）：")
    lines.append("  lwa autostart enable     # bootstrap 加载单元（KeepAlive 拉起前台进程）")
    lines.append("  lwa autostart check      # 深检解释器/PATH/进程/Caddy 是否完备")
    lines.append("")
    lines.append("或手动 launchctl：")
    for _name, path in written:
        lines.append(f"  launchctl bootstrap gui/$(id -u) {path}")
    lines.append("取消自启：")
    for _name, path in written:
        lines.append(f"  launchctl bootout gui/$(id -u)/$(basename {path} .plist)")
    lines.append("")
    lines.append(
        "提示：plist 以 KeepAlive 直接监管前台 watcher/uvicorn，崩溃即拉起（BUG-138）；"
        "停服前请先 `lwa autostart disable`，否则会被立刻拉回（030.b）。"
    )
    return "\n".join(lines)


__all__ = [
    "SetupItem",
    "SetupReport",
    "LAUNCHD_LABEL_PREFIX",
    "detect_platform",
    "format_setup_report",
    "format_autostart_report",
    "generate_launchd_plists",
    "render_setup_script",
    "run_setup",
]
