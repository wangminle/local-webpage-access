"""BUG-999/BUG-1000：buildProxy 保存的原子写权限与并发互斥。

* BUG-999：代理 URL 可能含凭据。原子写权限最多 0600，保留目标更严格的限制，
  且权限在 ``os.open`` 创建临时文件时一次落定——umask 022 下已有 0644 的
  配置也必须在保存凭据时收紧。
* BUG-1000：临时文件名带 UUID，并发保存不再共用同一临时文件；「读-改-校验-
  替换」全程由配置级文件锁保护，不再串写、丢更新或抛 ``FileNotFoundError``。
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest
import yaml

from local_webpage_access import build_proxy
from local_webpage_access.config import BuildProxy, example_config_text
from local_webpage_access.errors import ConfigError


def _config_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(example_config_text(), encoding="utf-8")
    path.chmod(mode)
    return path


def _save(path: Path, url: str) -> None:
    build_proxy.write_build_proxy(path, BuildProxy(enabled=True, url=url))


def test_existing_0600_config_keeps_mode_under_umask_022(tmp_path: Path) -> None:
    path = _config_file(tmp_path)
    old_mask = os.umask(0o022)
    try:
        _save(path, "http://user:secret@proxy.invalid:8080")
    finally:
        os.umask(old_mask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp-*"))


def test_new_config_defaults_to_0600(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    old_mask = os.umask(0o022)
    try:
        _save(path, "http://user:secret@proxy.invalid:8080")
    finally:
        os.umask(old_mask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp-*"))


@pytest.mark.parametrize("original, expected", [(0o644, 0o600), (0o640, 0o600), (0o400, 0o400)])
def test_existing_config_is_restricted_before_credentials_are_written(
    tmp_path: Path, monkeypatch, original: int, expected: int
) -> None:
    path = _config_file(tmp_path, original)
    real_replace = os.replace
    observed = []

    def replace(source, destination):
        # 替换前检查已经写入凭据的临时文件，排除「写完再 chmod」窗口。
        observed.append(stat.S_IMODE(Path(source).stat().st_mode))
        real_replace(source, destination)

    monkeypatch.setattr(build_proxy.os, "replace", replace)
    old_mask = os.umask(0o022)
    try:
        _save(path, "http://user:secret@proxy.invalid:8080")
    finally:
        os.umask(old_mask)
    assert observed == [expected]
    assert stat.S_IMODE(path.stat().st_mode) == expected


def test_concurrent_saves_are_serialized(tmp_path: Path) -> None:
    """两线程真实并发保存（不打补丁）：全部成功、无 FileNotFoundError、
    最终 YAML 可解析且为其中一方的完整值，其余键保留。"""
    path = _config_file(tmp_path)
    errors: list[BaseException] = []

    def worker(port: int) -> None:
        for round_ in range(10):
            try:
                _save(path, f"http://proxy-{port}.invalid:{port}/r{round_}")
            except BaseException as exc:  # noqa: BLE001 — 收集后统一断言
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(port,)) for port in (8080, 8081)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    expected = {
        f"http://proxy-{port}.invalid:{port}/r{round_}"
        for port in (8080, 8081)
        for round_ in range(10)
    }
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["buildProxy"]["enabled"] is True
    assert raw["buildProxy"]["url"] in expected
    assert not list(tmp_path.glob("*.tmp-*"))


def test_duplicate_block_rejected_without_disk_damage(tmp_path: Path) -> None:
    """校验失败（残留第二个 buildProxy 段）抛 ConfigError，磁盘保持原样、
    不留临时文件。"""
    path = _config_file(tmp_path)
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original + '\nbuildProxy:\n  enabled: false\n  url: ""\n', encoding="utf-8"
    )
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError, match="重复的 buildProxy 段"):
        _save(path, "http://proxy.invalid:8080")

    assert path.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob("*.tmp-*"))
