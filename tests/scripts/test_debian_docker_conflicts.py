"""Debian 发行版 Docker 升级 CE 前的冲突包清理与发行版边界。"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "src/local_webpage_access/scripts/install-docker-linux.sh"
)
_APT_CONFLICTS = (
    "docker.io",
    "docker-compose",
    "docker-compose-v2",
    "docker-doc",
    "podman-docker",
    "containerd",
    "runc",
)
_CE_PACKAGES = (
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
)
_UNRELATED = ("docker-unrelated", "docker-buildx-plugin", "docker-ce-cli")


def _install_commands(family: str, installed: tuple[str, ...]) -> list[list[str]]:
    """执行真实 main/清理/安装函数，仅模拟系统探测和系统操作。"""
    source = _SCRIPT.read_text(encoding="utf-8")
    invocation = '\nmain "$@"\n'
    assert source.endswith(invocation), "必须先移除入口调用，避免执行真实系统操作"
    source = source.removesuffix(invocation)
    stubs = r"""
need_cmd() { :; }
ensure_sudo() { :; }
check_apt_pkg() { :; }
already_good() { return 1; }
detect_distro_family() { printf '%s\n%s\n' "$LWA_TEST_FAMILY" test-release; }
setup_apt_repo() { :; }
setup_dnf_repo() { :; }
write_daemon_mirrors() { :; }
start_docker() { :; }
add_user_to_docker_group() { :; }
verify_install() { :; }
run_sudo() {
  printf 'COMMAND'
  printf ' %s' "$@"
  printf '\n'
}
dpkg() {
  [[ "$1" == "--get-selections" ]] || return 99
  shift
  local pkg
  for pkg in "$@"; do
    case " $LWA_TEST_INSTALLED " in
      *" $pkg "*) printf '%s\tinstall\n' "$pkg" ;;
    esac
  done
}
rpm() {
  [[ "$1" == "-q" ]] || return 99
  case " $LWA_TEST_INSTALLED " in
    *" $2 "*) return 0 ;;
    *) return 1 ;;
  esac
}
main
"""
    result = subprocess.run(
        ["bash", "-s"],
        input=source + "\n" + stubs,
        env={
            **os.environ,
            # macOS Bash 3.2 在 UTF-8 locale 下可能把中文标点误并入变量名。
            "LC_ALL": "C",
            "LWA_TEST_FAMILY": family,
            "LWA_TEST_INSTALLED": " ".join(installed),
            "LWA_APT_PKG_BROKEN": "0",
        },
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [
        shlex.split(line.removeprefix("COMMAND "))
        for line in result.stdout.splitlines()
        if line.startswith(("COMMAND apt-get ", "COMMAND dnf "))
    ]


@pytest.mark.parametrize(
    "conflicts",
    [
        ("docker-cli", "docker-buildx"),
        ("docker-cli",),
        ("docker-buildx",),
        (*_APT_CONFLICTS, "docker-cli", "docker-buildx"),
        (),
    ],
    ids=["cli-and-buildx", "cli-only", "buildx-only", "all-conflicts", "no-conflicts"],
)
def test_debian_removes_installed_conflicts_before_ce(conflicts: tuple[str, ...]) -> None:
    commands = _install_commands("debian", (*conflicts, *_UNRELATED))
    expected = []
    if conflicts:
        expected.append(["apt-get", "remove", "-y", *conflicts])
    expected.append(["apt-get", "install", "-y", *_CE_PACKAGES])
    assert commands == expected


def test_ubuntu_keeps_original_apt_conflict_boundary() -> None:
    installed = (*_APT_CONFLICTS, "docker-cli", "docker-buildx", *_UNRELATED)
    assert _install_commands("ubuntu", installed) == [
        ["apt-get", "remove", "-y", *_APT_CONFLICTS],
        ["apt-get", "install", "-y", *_CE_PACKAGES],
    ]


def test_fedora_keeps_original_dnf_conflict_boundary() -> None:
    conflicts = ("docker-cli", "moby-engine", "podman-docker")
    installed = (*conflicts, "containerd", "runc", "docker-buildx", *_UNRELATED)
    assert _install_commands("fedora", installed) == [
        ["dnf", "remove", "-y", *conflicts],
        ["dnf", "install", "-y", *_CE_PACKAGES],
    ]
