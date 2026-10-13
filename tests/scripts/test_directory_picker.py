"""IMP-051：宿主机原生目录选择器单测。"""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from typing import Any

import pytest

from local_webpage_access.directory_picker import pick_directory
from local_webpage_access.errors import DirectoryPickerError


def _ok(stdout: str, returncode: int = 0) -> CompletedProcess[str]:
    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestPickDirectoryDarwin:
    def test_osascript_success_returns_absolute_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("local_webpage_access.directory_picker.sys.platform", "darwin")

        def fake_run(cmd: list[str], **kwargs: Any) -> CompletedProcess[str]:
            assert cmd[0] == "osascript"
            return _ok("/Users/me/my-site/\n")

        path = pick_directory(runner=fake_run)
        assert path == "/Users/me/my-site"
        assert Path(path).is_absolute()

    def test_osascript_cancel_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("local_webpage_access.directory_picker.sys.platform", "darwin")

        def fake_run(cmd: list[str], **kwargs: Any) -> CompletedProcess[str]:
            return CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="execution error: User canceled. (-128)",
            )

        with pytest.raises(DirectoryPickerError) as exc:
            pick_directory(runner=fake_run)
        assert exc.value.code == "cancelled"


class TestPickDirectoryLinux:
    def test_zenity_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("local_webpage_access.directory_picker.sys.platform", "linux")
        monkeypatch.setattr(
            "local_webpage_access.directory_picker.shutil.which",
            lambda name: "/usr/bin/zenity" if name == "zenity" else None,
        )

        def fake_run(cmd: list[str], **kwargs: Any) -> CompletedProcess[str]:
            assert cmd[0] == "/usr/bin/zenity"
            assert "--directory" in cmd
            return _ok("/home/me/site\n")

        assert pick_directory(runner=fake_run) == "/home/me/site"

    def test_kdialog_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("local_webpage_access.directory_picker.sys.platform", "linux")
        monkeypatch.setattr(
            "local_webpage_access.directory_picker.shutil.which",
            lambda name: "/usr/bin/kdialog" if name == "kdialog" else None,
        )

        def fake_run(cmd: list[str], **kwargs: Any) -> CompletedProcess[str]:
            assert cmd[0] == "/usr/bin/kdialog"
            return _ok("/home/me/kde-site\n")

        assert pick_directory(runner=fake_run) == "/home/me/kde-site"

    def test_no_tool_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("local_webpage_access.directory_picker.sys.platform", "linux")
        monkeypatch.setattr(
            "local_webpage_access.directory_picker.shutil.which",
            lambda name: None,
        )
        with pytest.raises(DirectoryPickerError) as exc:
            pick_directory(runner=lambda *a, **k: _ok(""))
        assert exc.value.code == "unavailable"

    def test_zenity_cancel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("local_webpage_access.directory_picker.sys.platform", "linux")
        monkeypatch.setattr(
            "local_webpage_access.directory_picker.shutil.which",
            lambda name: "/usr/bin/zenity" if name == "zenity" else None,
        )

        def fake_run(cmd: list[str], **kwargs: Any) -> CompletedProcess[str]:
            return _ok("", returncode=1)

        with pytest.raises(DirectoryPickerError) as exc:
            pick_directory(runner=fake_run)
        assert exc.value.code == "cancelled"


class TestPickDirectoryCommon:
    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("local_webpage_access.directory_picker.sys.platform", "darwin")

        def fake_run(cmd: list[str], **kwargs: Any) -> CompletedProcess[str]:
            raise TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 1))

        with pytest.raises(DirectoryPickerError) as exc:
            pick_directory(runner=fake_run, timeout=1)
        assert exc.value.code == "timeout"

    def test_unsupported_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("local_webpage_access.directory_picker.sys.platform", "win32")
        with pytest.raises(DirectoryPickerError) as exc:
            pick_directory(runner=lambda *a, **k: _ok(""))
        assert exc.value.code == "unavailable"

    def test_empty_stdout_treated_as_cancel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("local_webpage_access.directory_picker.sys.platform", "darwin")

        def fake_run(cmd: list[str], **kwargs: Any) -> CompletedProcess[str]:
            return _ok("   \n", returncode=0)

        with pytest.raises(DirectoryPickerError) as exc:
            pick_directory(runner=fake_run)
        assert exc.value.code == "cancelled"
