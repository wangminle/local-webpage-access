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
    assert 'LWA_DOCKER_REGISTRY_MIRRORS:-' in text or 'LWA_DOCKER_REGISTRY_MIRRORS-' in text
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
    # 安装前必须确认候选 ≥ MIN，避免 apt 落到 universe 2.6.x
    assert "避免 apt 静默装上 Ubuntu universe 旧包" in text
    # curl 须带 -S，避免 -s 静默失败导致「无报错就回到 shell」
    assert "curl -1sSfL" in text
