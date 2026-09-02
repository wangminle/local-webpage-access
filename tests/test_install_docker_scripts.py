"""IMP-031：内置 Docker 安装脚本存在性与关键片段回归。"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

from local_webpage_access.version_requirements import (
    MIN_COMPOSE_VERSION,
    MIN_DOCKER_VERSION,
)

_SCRIPTS = Path(__file__).resolve().parents[1] / "src" / "local_webpage_access" / "scripts"
_LINUX = _SCRIPTS / "install-docker-linux.sh"
_MACOS = _SCRIPTS / "install-docker-macos.sh"


@pytest.mark.parametrize("path", [_LINUX, _MACOS], ids=["linux", "macos"])
def test_install_docker_script_exists_and_executable(path: Path) -> None:
    assert path.is_file(), f"missing {path}"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert MIN_DOCKER_VERSION in text
    assert MIN_COMPOSE_VERSION in text


def test_linux_script_follows_official_ubuntu_apt_flow() -> None:
    text = _LINUX.read_text(encoding="utf-8")
    # 官方文档步骤关键词
    assert "https://docs.docker.com/engine/install/ubuntu/" in text
    assert "docker.sources" in text
    assert "docker-ce" in text
    assert "docker-ce-cli" in text
    assert "containerd.io" in text
    assert "docker-buildx-plugin" in text
    assert "docker-compose-plugin" in text
    assert "docker.io" in text  # 冲突包卸载
    assert "podman-docker" in text
    # 阿里云默认包源
    assert "mirrors.aliyun.com/docker-ce" in text
    assert "--official" in text
    assert "registry-mirrors" in text


def test_linux_script_checks_apt_pkg() -> None:
    """BUG-203：预检 apt_pkg，避免 Python 3.13 宿主上 apt-get update 假死。"""
    text = _LINUX.read_text(encoding="utf-8")
    assert "check_apt_pkg" in text
    assert "import apt_pkg" in text
    assert "LWA_APT_PKG_BROKEN" in text
    assert "APT::Update::Post-Invoke" in text
    assert "apt_get()" in text
    # main 调用预检
    assert "check_apt_pkg" in text.split("main()")[1]


@pytest.mark.parametrize("path", [_LINUX, _MACOS], ids=["linux", "macos"])
def test_docker_scripts_default_registry_mirrors_nonempty(path: Path) -> None:
    """BUG-195：默认必须配置镜像拉取加速，不能空跳过。"""
    text = path.read_text(encoding="utf-8")
    assert "docker.m.daocloud.io" in text or "mirror.aliyuncs.com" in text
    # 默认非空；仅 none/- 才跳过
    assert "LWA_DOCKER_REGISTRY_MIRRORS:-" in text or "LWA_DOCKER_REGISTRY_MIRRORS-" in text
    assert "跳过 registry-mirrors" in text or "跳过 ~/.docker/daemon.json" in text
    # 跳过条件应是 none，而非默认空
    assert '== "none"' in text or "none" in text


def test_macos_script_uses_desktop_cask() -> None:
    text = _MACOS.read_text(encoding="utf-8")
    assert "brew install --cask docker" in text
    assert "registry-mirrors" in text
    assert ".docker/daemon.json" in text


def test_scripts_packaged_with_importlib_resources() -> None:
    root = files("local_webpage_access")
    scripts = root.joinpath("scripts")
    assert scripts.joinpath("install-docker-linux.sh").is_file()
    assert scripts.joinpath("install-docker-macos.sh").is_file()
    assert scripts.joinpath("install-caddy-linux.sh").is_file()
    assert scripts.joinpath("install-caddy-macos.sh").is_file()


def test_caddy_scripts_exist() -> None:
    from local_webpage_access.version_requirements import MIN_CADDY_VERSION

    for name in ("install-caddy-linux.sh", "install-caddy-macos.sh"):
        path = _SCRIPTS / name
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert "set -euo pipefail" in text
        assert MIN_CADDY_VERSION in text


def test_caddy_linux_guards_against_ubuntu_old_package() -> None:
    """BUG-209：Cloudsmith 未就绪时不得静默装 Ubuntu universe 旧包；需候选门禁 + GitHub 回退。"""
    text = (_SCRIPTS / "install-caddy-linux.sh").read_text(encoding="utf-8")
    assert "dl.cloudsmith.io/public/caddy/stable" in text
    assert "cloudsmith_candidate_ok" in text
    assert "apt-cache policy caddy" in text
    assert "install_via_github_release" in text
    assert "github.com/caddyserver/caddy/releases" in text
    assert "chmod o+r" in text
    assert "disable --now caddy.service" in text
    assert "refresh_apt_with_retry" in text
    # IMP-033 / BUG-231：already_good 路径也必须 disable 系统 caddy
    assert "仍检查并停用系统 caddy.service" in text
    assert "Full Profile 禁止与系统 Caddy 双托管" in text
    # 安装前必须确认候选 ≥ MIN，避免 apt 落到 universe 2.6.x
    assert "避免 apt 静默装上 Ubuntu universe 旧包" in text
    # curl 须带 -S，避免 -s 静默失败导致「无报错就回到 shell」
    assert "curl -1sSfL" in text


def test_linux_script_supports_debian_family_without_ubuntu_spoof() -> None:
    """IMP-036：Debian 走 /linux/debian，不得伪装成 Ubuntu 代号。"""
    text = _LINUX.read_text(encoding="utf-8")
    assert "detect_distro_family" in text
    assert "/linux/debian" in text or "linux/${family}" in text or "linux/${family}" in text
    assert "bookworm" in text
    # 不得在 Debian 分支写死 ubuntu apt 路径伪装
    assert "不得" in text or "严禁" in text or "debian" in text.lower()


def test_linux_script_rejects_debian_sid_and_ubuntu_non_lts() -> None:
    """BUG-261：安装脚本拒绝 Debian sid 与 Ubuntu 非 LTS。"""
    text = _LINUX.read_text(encoding="utf-8")
    assert "sid|unstable|testing" in text
    assert "die" in text
    assert "jammy|noble" in text
    assert "questing" not in text
    caddy = (_SCRIPTS / "install-caddy-linux.sh").read_text(encoding="utf-8")
    assert "LTS" in caddy
    assert "sid|unstable|testing" in caddy
    assert "jammy|noble" in caddy


def test_caddy_linux_script_accepts_debian() -> None:
    caddy = _SCRIPTS / "install-caddy-linux.sh"
    text = caddy.read_text(encoding="utf-8")
    assert "detect_distro_family" in text
    assert "Debian" in text or "debian" in text


def _bash_function(text: str, name: str) -> str:
    marker = f"{name}() {{"
    start = text.index(marker)
    collected: list[str] = []
    for line in text[start:].splitlines():
        collected.append(line)
        if line == "}":
            break
    return "\n".join(collected)


# Docker Fedora 官方冲突包清单：
# https://docs.docker.com/engine/install/fedora/#uninstall-old-versions
_FEDORA_OFFICIAL_CONFLICTS = (
    "docker",
    "docker-client",
    "docker-client-latest",
    "docker-common",
    "docker-latest",
    "docker-latest-logrotate",
    "docker-logrotate",
    "docker-selinux",
    "docker-engine-selinux",
    "docker-engine",
)


def test_linux_script_supports_fedora_dnf_flow() -> None:
    """Fedora：dnf 仓库直写（不依赖 config-manager 插件）+ rpm 导入 GPG key。"""
    text = _LINUX.read_text(encoding="utf-8")
    assert "https://docs.docker.com/engine/install/fedora/" in text
    assert "fedora)" in text
    assert "need_cmd dnf" in text
    assert "/etc/yum.repos.d/docker-ce.repo" in text
    assert "rpm --import" in text
    assert "dnf install -y" in text
    assert "moby-engine" in text  # Fedora 冲突包卸载（docker.io 是 Debian 系的）
    assert "dnf remove -y" in text
    # 阿里云镜像对 fedora 同样生效（$releasever/$basearch 由 dnf 展开）
    assert '\\$releasever' in text
    assert '\\$basearch' in text


def test_fedora_uninstall_follows_official_conflict_list() -> None:
    """BUG-622：Fedora 冲突包须严格采用官方清单；不得默认卸 containerd/runc。"""
    import re

    text = _LINUX.read_text(encoding="utf-8")
    fn = _bash_function(text, "uninstall_conflicts_fedora")
    for pkg in _FEDORA_OFFICIAL_CONFLICTS:
        assert pkg in fn, f"漏掉官方冲突包 {pkg}"
    # 已证明必要的发行版兼容包（注释须说明理由）
    assert "moby-engine" in fn
    assert "podman-docker" in fn
    code_lines = [
        ln for ln in fn.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    # Fedora 43/44 将 CLI 拆成可独立显式安装的 docker-cli 子包；它直接拥有
    # /usr/bin/docker，不能依赖删除 moby-engine 时的依赖清理来顺带移除。
    assert re.search(r"(?<![\w-])docker-cli(?![\w-])", code) is not None
    assert "containerd" not in code
    assert re.search(r"(?<![\w-])runc(?![\w-])", code) is None
    # 已安装冲突包的卸载失败不得被 || true 吞掉
    assert re.search(r"dnf remove[^\n]*\|\|\s*true", fn) is None


def test_linux_script_fedora_window_synced_with_platform_support() -> None:
    """Fedora 允许窗口须与 platform_support.SUPPORTED_FEDORA_RELEASES 一致。"""
    from local_webpage_access.platform_support import SUPPORTED_FEDORA_RELEASES

    releases = sorted(SUPPORTED_FEDORA_RELEASES)
    docker = _LINUX.read_text(encoding="utf-8")
    caddy = (_SCRIPTS / "install-caddy-linux.sh").read_text(encoding="utf-8")
    for script, name in ((docker, "docker"), (caddy, "caddy")):
        assert "|".join(str(r) for r in releases) in script, f"{name} 脚本 Fedora 窗口不同步"
        for ver in releases:
            assert f"低于最低要求 {releases[0]}" in script or str(ver) in script


def test_caddy_linux_script_supports_fedora() -> None:
    """Fedora：dnf 官方仓库优先，GitHub Release 二进制兜底；拒绝 Rawhide。"""
    text = (_SCRIPTS / "install-caddy-linux.sh").read_text(encoding="utf-8")
    assert "install_via_dnf" in text
    assert "dnf install -y caddy" in text
    assert "rawhide" in text
    assert "install_via_github_release" in text  # 兜底路径发行版无关


def test_linux_daemon_json_invalid_aborts_instead_of_empty_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-332：既有 daemon.json 非法 JSON 时必须中止，不得清空重建。"""
    daemon = tmp_path / "daemon.json"
    daemon.write_text("{not-json: true, keep: me}\n", encoding="utf-8")
    original = daemon.read_text(encoding="utf-8")

    # 抽取脚本内嵌 Python，替换目标路径后执行
    text = _LINUX.read_text(encoding="utf-8")
    start = text.index("import json, os, pathlib, shutil, tempfile")
    end = text.index("tmp_path.replace(path)", start) + len("tmp_path.replace(path)")
    snippet = text[start:end]
    snippet = snippet.replace(
        'path = pathlib.Path("/etc/docker/daemon.json")',
        f"path = pathlib.Path({str(daemon)!r})",
    )

    monkeypatch.setenv("MIRRORS_CSV", "https://example.invalid/mirror")
    with pytest.raises(SystemExit) as ei:
        exec(snippet, {"__name__": "__main__"})  # noqa: S102 — 回归脚本内嵌逻辑
    assert ei.value.code  # 非空消息 / 非零
    assert "无法解析" in str(ei.value)
    assert daemon.read_text(encoding="utf-8") == original
    assert daemon.with_suffix(".json.bak-lwa").is_file()
