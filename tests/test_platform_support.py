"""IMP-036：正式支持平台矩阵、门禁与 Windows 原生 fail-fast。"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access.platform_support import (
    MACOS_MIN_MAJOR,
    MIN_GLIBC_VERSION,
    MIN_KERNEL_VERSION,
    MIN_WSL_PACKAGE_VERSION,
    PlatformSupportReport,
    collect_platform_support_report,
    is_wsl_drvfs_path,
    require_supported_platform,
)


def _supported_ubuntu(**overrides):
    base = dict(
        platform_name="linux",
        distro_id="ubuntu",
        distro_version="22.04",
        kernel_version="5.15.0",
        libc_version="2.35",
        architecture="x86_64",
        wsl_version=None,
        wsl_package_version=None,
        systemd_available=True,
        systemd_pid1=True,
    )
    base.update(overrides)
    return collect_platform_support_report(**base)


def test_report_fields_always_present_when_unsupported() -> None:
    report = collect_platform_support_report(
        platform_name="windows",
        distro_id=None,
        distro_version=None,
        kernel_version=None,
        libc_version=None,
        architecture="AMD64",
        wsl_version=None,
        systemd_available=False,
        systemd_pid1=False,
    )
    assert isinstance(report, PlatformSupportReport)
    payload = report.to_dict()
    for key in (
        "platform",
        "distroId",
        "distroVersion",
        "kernelVersion",
        "libcVersion",
        "architecture",
        "wslVersion",
        "systemdAvailable",
        "supported",
        "reasons",
        "action",
    ):
        assert key in payload
    assert payload["supported"] is False
    assert payload["platform"] == "windows"
    assert "WSL2" in (payload["action"] or "")
    assert any("Windows" in r or "原生" in r for r in payload["reasons"])


def test_windows_native_hard_fail() -> None:
    report = collect_platform_support_report(
        platform_name="windows",
        architecture="x86_64",
        systemd_available=False,
    )
    assert report.supported is False
    with pytest.raises(SystemExit) as exc:
        require_supported_platform(report=report)
    assert exc.value.code != 0


def test_wsl2_not_treated_as_windows() -> None:
    report = collect_platform_support_report(
        platform_name="wsl",
        distro_id="ubuntu",
        distro_version="24.04",
        kernel_version="5.15.167.4-microsoft-standard-WSL2",
        libc_version="2.39",
        architecture="x86_64",
        wsl_version="2",
        wsl_package_version="2.1.5",
        systemd_available=True,
        systemd_pid1=True,
    )
    assert report.platform == "wsl"
    assert report.supported is True


def test_wsl1_rejected() -> None:
    report = collect_platform_support_report(
        platform_name="wsl",
        distro_id="ubuntu",
        distro_version="22.04",
        kernel_version="4.4.0-19041-Microsoft",
        libc_version="2.35",
        architecture="x86_64",
        wsl_version="1",
        wsl_package_version="1.0.0",
        systemd_available=False,
        systemd_pid1=False,
    )
    assert report.supported is False
    assert any("WSL1" in r or "WSL 1" in r for r in report.reasons)


def test_old_kernel_rejected() -> None:
    report = _supported_ubuntu(kernel_version="5.14.0")
    assert report.supported is False
    assert any(MIN_KERNEL_VERSION in r for r in report.reasons)


def test_old_glibc_rejected() -> None:
    report = _supported_ubuntu(libc_version="2.34")
    assert report.supported is False
    assert any(MIN_GLIBC_VERSION in r for r in report.reasons)


def test_unsupported_arch_rejected() -> None:
    report = _supported_ubuntu(architecture="armv7l")
    assert report.supported is False
    assert any("架构" in r or "armv7" in r.lower() for r in report.reasons)


def test_old_ubuntu_rejected() -> None:
    report = _supported_ubuntu(distro_version="20.04")
    assert report.supported is False


def test_debian_12_supported() -> None:
    report = collect_platform_support_report(
        platform_name="linux",
        distro_id="debian",
        distro_version="12",
        kernel_version="6.1.0",
        libc_version="2.36",
        architecture="aarch64",
        systemd_available=True,
        systemd_pid1=True,
    )
    assert report.supported is True


def test_debian_11_rejected() -> None:
    report = collect_platform_support_report(
        platform_name="linux",
        distro_id="debian",
        distro_version="11",
        kernel_version="5.15.0",
        libc_version="2.35",
        architecture="x86_64",
        systemd_available=True,
        systemd_pid1=True,
    )
    assert report.supported is False


def test_fedora_rejected() -> None:
    report = collect_platform_support_report(
        platform_name="linux",
        distro_id="fedora",
        distro_version="40",
        kernel_version="6.8.0",
        libc_version="2.39",
        architecture="x86_64",
        systemd_available=True,
        systemd_pid1=True,
    )
    assert report.supported is False


def test_macos_sonoma_supported() -> None:
    report = collect_platform_support_report(
        platform_name="macos",
        distro_id=None,
        distro_version=str(MACOS_MIN_MAJOR),
        kernel_version="23.0.0",
        libc_version=None,
        architecture="arm64",
        systemd_available=False,
        systemd_pid1=False,
    )
    assert report.supported is True


def test_macos_below_rolling_min_rejected() -> None:
    report = collect_platform_support_report(
        platform_name="macos",
        distro_version=str(MACOS_MIN_MAJOR - 1),
        architecture="x86_64",
    )
    assert report.supported is False
    assert any(str(MACOS_MIN_MAJOR) in r or "macOS" in r for r in report.reasons)


def test_wsl_old_package_rejected() -> None:
    report = collect_platform_support_report(
        platform_name="wsl",
        distro_id="ubuntu",
        distro_version="22.04",
        kernel_version="5.15.0",
        libc_version="2.35",
        architecture="x86_64",
        wsl_version="2",
        wsl_package_version="2.0.0",
        systemd_available=True,
        systemd_pid1=True,
    )
    assert report.supported is False
    assert any(MIN_WSL_PACKAGE_VERSION in r for r in report.reasons)


def test_wsl_without_systemd_pid1_rejected() -> None:
    report = collect_platform_support_report(
        platform_name="wsl",
        distro_id="ubuntu",
        distro_version="22.04",
        kernel_version="5.15.0",
        libc_version="2.35",
        architecture="x86_64",
        wsl_version="2",
        wsl_package_version="2.1.5",
        systemd_available=True,
        systemd_pid1=False,
    )
    assert report.supported is False
    assert any("systemd" in r.lower() or "PID 1" in r or "PID1" in r for r in report.reasons)


def test_wsl_unknown_package_version_fail_closed() -> None:
    report = collect_platform_support_report(
        platform_name="wsl",
        distro_id="ubuntu",
        distro_version="22.04",
        kernel_version="5.15.0",
        libc_version="2.35",
        architecture="x86_64",
        wsl_version="2",
        wsl_package_version="unknown",
        systemd_available=True,
        systemd_pid1=True,
    )
    assert report.supported is False
    assert report.wsl_version == "unknown"
    assert any("unknown" in r.lower() or "无法" in r for r in report.reasons)


def test_drvfs_workspace_path_detected() -> None:
    assert is_wsl_drvfs_path(Path("/mnt/c/Users/foo/lwa")) is True
    assert is_wsl_drvfs_path(Path("/home/foo/lwa")) is False


def test_wsl_drvfs_does_not_mark_platform_unsupported() -> None:
    """BUG-260：/mnt/<drive> 不得并入全局 unsupported（仅 Full/autostart 写路径阻断）。"""
    report = collect_platform_support_report(
        platform_name="wsl",
        distro_id="ubuntu",
        distro_version="22.04",
        kernel_version="5.15.0",
        libc_version="2.35",
        architecture="x86_64",
        wsl_version="2",
        wsl_package_version="2.1.5",
        systemd_available=True,
        systemd_pid1=True,
        workspace_root="/mnt/c/Users/foo/lwa",
    )
    assert report.workspace_on_drvfs is True
    assert report.supported is True
    assert not any("/mnt" in r for r in report.reasons)


def test_drvfs_not_inferred_from_cwd_without_workspace_root(monkeypatch) -> None:
    """BUG-260：未传 workspace_root 时不得用 cwd 判定 drvfs 并污染报告。"""
    monkeypatch.setattr(
        "local_webpage_access.platform_support.Path.cwd",
        classmethod(lambda cls: Path("/mnt/c/Users/foo")),
    )
    report = collect_platform_support_report(
        platform_name="wsl",
        distro_id="ubuntu",
        distro_version="22.04",
        kernel_version="5.15.0",
        libc_version="2.35",
        architecture="x86_64",
        wsl_version="2",
        wsl_package_version="2.1.5",
        systemd_available=True,
        systemd_pid1=True,
        workspace_root=None,
    )
    assert report.workspace_on_drvfs is None


def test_assert_writable_workspace_blocks_drvfs() -> None:
    """BUG-260：Full/autostart 写路径对 /mnt/<drive> fail-closed。"""
    from local_webpage_access.platform_support import assert_writable_workspace_allowed

    report = collect_platform_support_report(
        platform_name="wsl",
        distro_id="ubuntu",
        distro_version="22.04",
        kernel_version="5.15.0",
        libc_version="2.35",
        architecture="x86_64",
        wsl_version="2",
        wsl_package_version="2.1.5",
        systemd_available=True,
        systemd_pid1=True,
        workspace_root="/mnt/d/proj",
    )
    with pytest.raises(SystemExit):
        assert_writable_workspace_allowed("/mnt/d/proj", report=report)


def test_assert_writable_allows_drvfs_on_native_linux() -> None:
    """BUG-260：原生 Linux 上 /mnt/c 不得因路径形似 drvfs 被误挡。"""
    from local_webpage_access.platform_support import assert_writable_workspace_allowed

    report = collect_platform_support_report(
        platform_name="linux",
        distro_id="ubuntu",
        distro_version="22.04",
        kernel_version="5.15.0",
        libc_version="2.35",
        architecture="x86_64",
        systemd_available=True,
        systemd_pid1=True,
        workspace_root="/mnt/c/lwa",
    )
    assert_writable_workspace_allowed("/mnt/c/lwa", report=report)


def test_run_full_bootstrap_drvfs_only_blocks_on_wsl(monkeypatch, tmp_path: Path) -> None:
    """BUG-260：run_full_bootstrap 仅在 WSL + drvfs 时阻断，原生 Linux 放行路径检查。"""
    from local_webpage_access.host_bootstrap import run_full_bootstrap
    import local_webpage_access.platform_support as ps

    monkeypatch.setattr(ps, "is_wsl_drvfs_path", lambda _p: True)

    result_linux = run_full_bootstrap(
        platform="linux",
        yes=True,
        workspace_root=tmp_path,
    )
    assert not any("/mnt/<drive>" in m or "Windows 文件系统" in m for m in result_linux.messages)

    result_wsl = run_full_bootstrap(
        platform="wsl",
        yes=True,
        workspace_root=tmp_path,
    )
    assert result_wsl.ok is False
    assert any("/mnt/<drive>" in m or "Windows 文件系统" in m for m in result_wsl.messages)


def test_init_full_blocks_drvfs_before_writing(monkeypatch, tmp_path: Path) -> None:
    """BUG-260：init --full 在 WSL+/mnt 上须于 init_workspace 前 fail-closed。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    import local_webpage_access.platform_support as ps

    target = tmp_path / "mnt" / "c" / "lwa"
    # 用真实子目录模拟目标路径；门禁靠 is_wsl_drvfs_path mock
    monkeypatch.setattr(ps, "is_wsl_drvfs_path", lambda _p: True)
    monkeypatch.setattr(
        ps,
        "detect_platform",
        lambda: "wsl",
    )
    # 根门禁放行，避免 Windows/unsupported 先挡
    monkeypatch.setattr(
        ps,
        "collect_platform_support_report",
        lambda **_k: collect_platform_support_report(
            platform_name="wsl",
            distro_id="ubuntu",
            distro_version="22.04",
            kernel_version="5.15.0",
            libc_version="2.35",
            architecture="x86_64",
            wsl_version="2",
            wsl_package_version="2.1.5",
            systemd_available=True,
            systemd_pid1=True,
            workspace_root=str(target),
        ),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["init", "-w", str(target), "--full", "--yes"])
    assert result.exit_code != 0
    assert not (target / "local-web.yml").exists()
    assert not (target / "registry").exists()
    blob = result.output + (result.stderr or "")
    assert "/mnt" in blob or "文件系统" in blob or "阻断" in blob


def test_ubuntu_future_lts_not_in_matrix_rejected() -> None:
    """BUG-261：尚未纳入发布矩阵的 Ubuntu 28.04 不得假绿。"""
    report = _supported_ubuntu(distro_version="28.04")
    assert report.supported is False


def test_debian_version_codename_mismatch_rejected() -> None:
    """BUG-261：Debian 12 + bullseye（版本/代号不一致）不得判支持。"""
    report = collect_platform_support_report(
        platform_name="linux",
        distro_id="debian",
        distro_version="12",
        distro_codename="bullseye",
        kernel_version="6.1.0",
        libc_version="2.36",
        architecture="x86_64",
        systemd_available=True,
        systemd_pid1=True,
    )
    assert report.supported is False


def test_ubuntu_version_codename_mismatch_rejected() -> None:
    """BUG-261：Ubuntu 22.04 + noble 不一致不得判支持。"""
    report = collect_platform_support_report(
        platform_name="linux",
        distro_id="ubuntu",
        distro_version="22.04",
        distro_codename="noble",
        kernel_version="5.15.0",
        libc_version="2.35",
        architecture="x86_64",
        systemd_available=True,
        systemd_pid1=True,
    )
    assert report.supported is False


def test_distro_matrix_shared_with_install_scripts() -> None:
    """BUG-261：平台报告与安装脚本共用同一版本/代号允许矩阵。"""
    from local_webpage_access.platform_support import (
        SUPPORTED_DEBIAN_STABLE,
        SUPPORTED_UBUNTU_LTS,
    )

    root = Path(__file__).resolve().parents[1]
    docker = (root / "src/local_webpage_access/scripts/install-docker-linux.sh").read_text(
        encoding="utf-8"
    )
    caddy = (root / "src/local_webpage_access/scripts/install-caddy-linux.sh").read_text(
        encoding="utf-8"
    )
    for code in SUPPORTED_UBUNTU_LTS.values():
        assert code in docker
        assert code in caddy
    for code in SUPPORTED_DEBIAN_STABLE.values():
        assert code in docker
        assert code in caddy
    # 允许列表须为矩阵代号，不得把非矩阵代号放进成功分支
    assert "jammy|noble|resolute" in docker
    assert "bookworm|trixie" in docker
    assert "questing" not in docker
    assert "sid|unstable|testing" in docker
    # 未来未纳入矩阵的 LTS 须显式拒绝（文案可提及 28.04）
    assert "major" in docker and "26" in docker


def test_ubuntu_non_lts_rejected() -> None:
    """BUG-261：Ubuntu 非 LTS（如 23.10）不得判定支持。"""
    report = _supported_ubuntu(distro_version="23.10")
    assert report.supported is False
    assert any("LTS" in r or "23.10" in r for r in report.reasons)


def test_ubuntu_lts_24_04_supported() -> None:
    report = _supported_ubuntu(distro_version="24.04")
    assert report.supported is True


def test_debian_sid_codename_rejected() -> None:
    """BUG-261：Debian sid/unstable 不得判定支持。"""
    report = collect_platform_support_report(
        platform_name="linux",
        distro_id="debian",
        distro_version="",
        distro_codename="sid",
        kernel_version="6.1.0",
        libc_version="2.36",
        architecture="x86_64",
        systemd_available=True,
        systemd_pid1=True,
    )
    assert report.supported is False
    assert any("sid" in r.lower() or "Stable" in r or "稳定" in r for r in report.reasons)


def test_debian_future_major_rejected() -> None:
    """BUG-261：未知未来 Debian 大版本（如 99）不得假绿。"""
    report = collect_platform_support_report(
        platform_name="linux",
        distro_id="debian",
        distro_version="99",
        kernel_version="6.1.0",
        libc_version="2.36",
        architecture="x86_64",
        systemd_available=True,
        systemd_pid1=True,
    )
    assert report.supported is False


# ---- Fedora（滚动窗口：当前 + 前一稳定版）----------------------------------------


def _supported_fedora(**overrides):
    base = dict(
        platform_name="linux",
        distro_id="fedora",
        distro_version="44",
        kernel_version="6.15.0",
        libc_version="2.41",
        architecture="x86_64",
        systemd_available=True,
        systemd_pid1=True,
    )
    base.update(overrides)
    return collect_platform_support_report(**base)


def test_fedora_matrix_releases_supported() -> None:
    from local_webpage_access.platform_support import SUPPORTED_FEDORA_RELEASES

    assert SUPPORTED_FEDORA_RELEASES, "Fedora 滚动窗口不得为空"
    for ver in sorted(SUPPORTED_FEDORA_RELEASES):
        report = _supported_fedora(distro_version=str(ver))
        assert report.supported is True, report.reasons


def test_fedora_previous_window_release_supported() -> None:
    """前一稳定版（43）在窗口内；VERSION_CODENAME 为空不影响判定。"""
    report = _supported_fedora(distro_version="43", distro_codename="")
    assert report.supported is True


def test_fedora_rawhide_rejected() -> None:
    report = _supported_fedora(distro_version="44", distro_codename="Rawhide")
    assert report.supported is False
    assert any("Rawhide" in r for r in report.reasons)


def test_fedora_future_release_rejected() -> None:
    """未纳入矩阵的未来 Fedora（如 45）不得假绿，须提示随矩阵滚动更新。"""
    report = _supported_fedora(distro_version="45")
    assert report.supported is False
    assert any("45" in r and "支持矩阵" in r for r in report.reasons)


def test_fedora_old_release_rejected() -> None:
    report = _supported_fedora(distro_version="42")
    assert report.supported is False


def test_fedora_version_with_patch_suffix_parsed() -> None:
    """VERSION_ID 理论上可带后缀（如 44.1）；按大版本对照窗口。"""
    report = _supported_fedora(distro_version="44.1")
    assert report.supported is True


def test_user_facing_docs_include_fedora_in_supported_matrix() -> None:
    """BUG-623：对外口径不得把 Fedora 列为不支持，也不得从安装矩阵遗漏。"""
    root = Path(__file__).resolve().parents[1]
    known = (root / "docs" / "known-limitations.md").read_text(encoding="utf-8")
    assert "Fedora/RHEL/Arch 等未纳入矩阵" not in known
    assert "Fedora" in known
    assert "43" in known and "44" in known
    faq = (root / "docs" / "faq.md").read_text(encoding="utf-8")
    assert "Fedora" in faq
    assert "43" in faq and "44" in faq
    skill = (
        root
        / "src"
        / "local_webpage_access"
        / "skills"
        / "lwa-setup-host-environment"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "Fedora" in skill
    assert "43" in skill and "44" in skill
    checklist = (root / "docs" / "release-checklist.md").read_text(encoding="utf-8")
    assert "Fedora" in checklist


def test_cli_gate_blocks_init_on_windows(monkeypatch, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    import local_webpage_access.platform_support as ps

    monkeypatch.setattr(
        ps,
        "collect_platform_support_report",
        lambda **_k: collect_platform_support_report(
            platform_name="windows",
            architecture="AMD64",
            systemd_available=False,
        ),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["init", "-w", str(tmp_path / "ws")])
    assert result.exit_code != 0
    assert "WSL2" in (result.output + (result.stderr or ""))
    assert not (tmp_path / "ws" / "local-web.yml").exists()


def test_cli_version_and_help_allowed_on_windows(monkeypatch) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    import local_webpage_access.platform_support as ps

    monkeypatch.setattr(
        ps,
        "collect_platform_support_report",
        lambda **_k: collect_platform_support_report(
            platform_name="windows",
            architecture="AMD64",
            systemd_available=False,
        ),
    )
    runner = CliRunner()
    assert runner.invoke(app, ["version"]).exit_code == 0
    assert runner.invoke(app, ["--help"]).exit_code == 0


def test_doctor_json_includes_platform_support(monkeypatch, tmp_path: Path) -> None:
    import json

    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    from local_webpage_access.init_workspace import init_workspace
    import local_webpage_access.platform_support as ps

    root = tmp_path / "ws"
    init_workspace(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        ps,
        "collect_platform_support_report",
        lambda **_k: collect_platform_support_report(
            platform_name="linux",
            distro_id="ubuntu",
            distro_version="22.04",
            kernel_version="5.15.0",
            libc_version="2.35",
            architecture="x86_64",
            systemd_available=True,
            systemd_pid1=True,
        ),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code in (0, 1)
    payload = json.loads(result.stdout or result.output)
    assert "platformSupport" in payload
    ps_payload = payload["platformSupport"]
    for key in (
        "platform",
        "distroId",
        "distroVersion",
        "kernelVersion",
        "libcVersion",
        "architecture",
        "wslVersion",
        "systemdAvailable",
        "supported",
        "reasons",
        "action",
    ):
        assert key in ps_payload


def test_doctor_json_platform_support_without_workspace(monkeypatch, tmp_path: Path) -> None:
    """BUG-262：未初始化工作区时 doctor --json 仍须输出 platformSupport。"""
    import json

    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    import local_webpage_access.platform_support as ps

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setattr(
        ps,
        "collect_platform_support_report",
        lambda **_k: collect_platform_support_report(
            platform_name="linux",
            distro_id="ubuntu",
            distro_version="22.04",
            kernel_version="5.15.0",
            libc_version="2.35",
            architecture="x86_64",
            systemd_available=True,
            systemd_pid1=True,
        ),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code != 0
    payload = json.loads(result.stdout or result.output)
    assert "platformSupport" in payload
    assert payload["platformSupport"]["supported"] is True
    assert payload["platformSupport"]["platform"] == "linux"


def test_import_package_does_not_exit() -> None:
    """import 包不得因平台门禁 sys.exit。"""
    import importlib

    import local_webpage_access.platform_support as ps

    importlib.reload(ps)
    assert hasattr(ps, "collect_platform_support_report")


def test_detect_wsl_package_version_parses_utf16le_bytes(monkeypatch) -> None:
    """BUG-282：wsl.exe 默认 UTF-16LE 输出须能解析出包版本。"""
    from types import SimpleNamespace

    import local_webpage_access.platform_support as ps
    from local_webpage_access.platform_support import detect_wsl_package_version

    sample = "WSL version: 2.4.13.0\nKernel version: 5.15.167.4-1\nWSLg version: 1.0.65\n"
    raw = sample.encode("utf-16-le")

    def runner(args, **kwargs):  # noqa: ANN001, ANN003
        assert kwargs.get("text") is not True  # 须以 bytes 捕获
        env = kwargs.get("env") or {}
        assert env.get("WSL_UTF8") == "1"
        assert "WSL_UTF8" in str(env.get("WSLENV", ""))
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")

    monkeypatch.setattr(ps, "detect_platform", lambda: "wsl")
    assert detect_wsl_package_version(runner=runner) == "2.4.13.0"


def test_detect_wsl_package_version_parses_utf8_and_zh(monkeypatch) -> None:
    """BUG-282：UTF-8 / 中文「WSL 版本」行亦可解析。"""
    from types import SimpleNamespace

    import local_webpage_access.platform_support as ps
    from local_webpage_access.platform_support import detect_wsl_package_version

    sample = "WSL 版本: 2.1.5.0\n内核版本: 5.15.0\n".encode("utf-8")

    def runner(args, **kwargs):  # noqa: ANN001, ANN003
        return SimpleNamespace(returncode=0, stdout=sample, stderr=b"")

    monkeypatch.setattr(ps, "detect_platform", lambda: "wsl")
    assert detect_wsl_package_version(runner=runner) == "2.1.5.0"


def test_detect_wsl_package_version_never_reports_kernel_as_package(
    monkeypatch,
) -> None:
    """BUG-288：只有 Kernel/WSLg 行时不得把内核版本当包版本（会绕过 ≥2.1.5 门禁）。"""
    from types import SimpleNamespace

    import local_webpage_access.platform_support as ps
    from local_webpage_access.platform_support import detect_wsl_package_version

    sample = b"Kernel version: 5.15.167.4-1\nWSLg version: 1.0.65\n"

    def runner(args, **kwargs):  # noqa: ANN001, ANN003
        return SimpleNamespace(returncode=0, stdout=sample, stderr=b"")

    monkeypatch.setattr(ps, "detect_platform", lambda: "wsl")
    assert detect_wsl_package_version(runner=runner) == "unknown"


def test_detect_wsl_package_version_retries_runner_without_env(monkeypatch) -> None:
    """BUG-288：注入的 runner 不接受 env= 时应降级重试，而非静默判 unknown。"""
    from types import SimpleNamespace

    import local_webpage_access.platform_support as ps
    from local_webpage_access.platform_support import detect_wsl_package_version

    def runner(args, capture_output=True, text=False, timeout=5, check=False):  # noqa: ANN001
        return SimpleNamespace(returncode=0, stdout=b"WSL version: 2.4.13.0\n", stderr=b"")

    monkeypatch.setattr(ps, "detect_platform", lambda: "wsl")
    assert detect_wsl_package_version(runner=runner) == "2.4.13.0"


def test_wsl_exe_candidates_scans_drvfs_when_not_on_path(monkeypatch, tmp_path: Path) -> None:
    """BUG-291：PATH 无 wsl.exe 时扫描 /mnt/<drive>/Windows/System32/wsl.exe。"""
    import local_webpage_access.platform_support as ps

    exe = tmp_path / "c" / "Windows" / "System32" / "wsl.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")

    monkeypatch.setattr(ps.shutil, "which", lambda _name: None)
    real_path = ps.Path

    def fake_path(*args, **kwargs):  # noqa: ANN002, ANN003
        if args and str(args[0]) in ("/mnt", "/mnt/"):
            return real_path(tmp_path)
        return real_path(*args, **kwargs)

    monkeypatch.setattr(ps, "Path", fake_path)
    found = ps._wsl_exe_candidates()
    assert found == [str(exe)]


def test_detect_wsl_package_version_falls_back_to_system32_path(
    monkeypatch,
) -> None:
    """BUG-291：PATH 名失败后继续尝试绝对路径候选，仍可解析包版本。"""
    from types import SimpleNamespace

    import local_webpage_access.platform_support as ps
    from local_webpage_access.platform_support import detect_wsl_package_version

    abs_exe = "/mnt/c/Windows/System32/wsl.exe"
    sample = b"WSL version: 2.9.4.0\nKernel version: 6.18.35.2-1\n"

    def runner(args, **kwargs):  # noqa: ANN001, ANN003
        if args[0] == "wsl.exe":
            raise FileNotFoundError("wsl.exe")
        assert args[0] == abs_exe
        return SimpleNamespace(returncode=0, stdout=sample, stderr=b"")

    monkeypatch.setattr(ps, "detect_platform", lambda: "wsl")
    monkeypatch.setattr(ps, "_wsl_exe_candidates", lambda: ["wsl.exe", abs_exe])
    assert detect_wsl_package_version(runner=runner) == "2.9.4.0"


def test_detect_wsl_package_version_unknown_when_runner_fails(monkeypatch) -> None:
    """BUG-282：interop 失败返回 unknown，不得抛异常。"""
    import local_webpage_access.platform_support as ps
    from local_webpage_access.platform_support import detect_wsl_package_version

    def runner(args, **kwargs):  # noqa: ANN001, ANN003
        raise FileNotFoundError("wsl.exe")

    monkeypatch.setattr(ps, "detect_platform", lambda: "wsl")
    assert detect_wsl_package_version(runner=runner) == "unknown"


def test_doctor_human_prints_platform_support_when_unsupported(monkeypatch, tmp_path: Path) -> None:
    """BUG-283：人类可读 doctor 须打印平台 reasons，不能只静默 exit 1。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    from local_webpage_access.init_workspace import init_workspace
    import local_webpage_access.platform_support as ps

    root = tmp_path / "ws"
    init_workspace(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        ps,
        "collect_platform_support_report",
        lambda **_k: collect_platform_support_report(
            platform_name="wsl",
            distro_id="ubuntu",
            distro_version="22.04",
            kernel_version="5.15.0-microsoft-standard-WSL2",
            libc_version="2.35",
            architecture="x86_64",
            wsl_version="2",
            wsl_package_version="unknown",
            systemd_available=True,
            systemd_pid1=True,
        ),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    out = (result.stdout or "") + (result.output or "")
    assert result.exit_code != 0
    assert "平台支持" in out or "platform" in out.lower()
    assert "unknown" in out.lower() or "无法确定" in out
    assert "wsl" in out.lower() or "WSL" in out
