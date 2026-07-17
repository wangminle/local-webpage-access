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
