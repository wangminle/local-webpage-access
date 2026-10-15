"""IMP-067：nginx 收敛为兼容读取（新写入拒绝 / 旧 YAML 映射 / update 持久迁移）。"""

from __future__ import annotations

import os

import pytest

from local_webpage_access.config import Config, load_config
from local_webpage_access.updater import migrate_config_defaults


def test_new_config_model_rejects_nginx() -> None:
    """067.a：严格模型不再接受 nginx，报错带迁移指引。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="nginx 从未实现"):
        Config(staticGateway="nginx")


def test_cli_enum_has_no_nginx() -> None:
    """067.a：init/setup 的网关枚举仅 caddy | builtin。"""
    from local_webpage_access.cli import StaticGatewayChoice

    assert {m.value for m in StaticGatewayChoice} == {"caddy", "builtin"}


def test_legacy_yaml_maps_builtin_once_without_rewrite(
    workspace, monkeypatch, caplog
) -> None:
    """067.b/067.d：旧 YAML 按 builtin 兼容；单进程一次警告；加载不改文件。"""
    import logging

    import local_webpage_access.config as config_mod

    # 全局「单进程一次」标志会被同进程先前的用例消耗——本用例验证的是首次
    # 警告行为，显式复位以保证与执行顺序无关。
    monkeypatch.setattr(config_mod, "_LEGACY_GATEWAY_WARNED", False)
    workspace.config_path.write_text(
        "managerPort: 17800\nstaticGateway: nginx  # 历史值\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="local_webpage_access.config"):
        cfg = load_config(workspace)
        assert cfg.staticGateway == "builtin"
        warnings_first = [
            r for r in caplog.records if "nginx" in r.getMessage()
        ]
        assert len(warnings_first) == 1
        load_config(workspace)  # 同进程第二次不再警告
        assert len([r for r in caplog.records if "nginx" in r.getMessage()]) == 1
    # 加载阶段不改写文件
    assert "staticGateway: nginx" in workspace.config_path.read_text(encoding="utf-8")


def test_other_invalid_gateway_still_fails_closed(workspace) -> None:
    workspace.config_path.write_text(
        "managerPort: 17800\nstaticGateway: traefik\n", encoding="utf-8"
    )
    with pytest.raises(Exception, match="staticGateway"):
        load_config(workspace)


def test_updater_persists_nginx_migration_with_backup(workspace) -> None:
    """067.c：update 迁移阶段备份后原子写回 builtin；可重跑幂等。"""
    workspace.config_path.write_text(
        "managerPort: 17800\nportPool:\n  start: 19000\n  end: 19100\n"
        "staticGateway: nginx  # 历史值\n",
        encoding="utf-8",
    )
    cfg = load_config(workspace)
    missing, written = migrate_config_defaults(workspace, cfg)

    assert written is True
    assert any("nginx→builtin" in m for m in missing)
    raw = workspace.config_path.read_text(encoding="utf-8")
    assert "staticGateway: builtin" in raw and "nginx  # 历史值" not in raw
    # 行内注释保留
    assert "staticGateway: builtin  # 历史值" in raw
    # 备份保留原文件
    backup = workspace.config_path.with_suffix(".yml.bak")
    assert "staticGateway: nginx" in backup.read_text(encoding="utf-8")

    # 重跑幂等：无 nginx、无缺失键 → 不再写盘
    missing2, written2 = migrate_config_defaults(workspace, load_config(workspace))
    assert written2 is False and missing2 == []


def test_updater_does_not_rewrite_nginx_lookalikes(workspace) -> None:
    """``nginx-custom`` / ``nginx2`` 不是历史值，迁移不得改写。"""
    original = (
        "managerPort: 17800\nportPool:\n  start: 19000\n  end: 19100\n"
        "staticGateway: nginx-custom\n"
    )
    workspace.config_path.write_text(original, encoding="utf-8")
    missing, _written = migrate_config_defaults(workspace, Config())
    raw = workspace.config_path.read_text(encoding="utf-8")
    assert "staticGateway: nginx-custom" in raw
    assert "builtin" not in raw.split("staticGateway:", 1)[1].splitlines()[0]
    assert not any("nginx→builtin" in item for item in missing)

    workspace.config_path.write_text(
        "managerPort: 17800\nportPool:\n  start: 19000\n  end: 19100\n"
        "staticGateway: nginx2\n",
        encoding="utf-8",
    )
    missing2, _written2 = migrate_config_defaults(workspace, Config())
    raw2 = workspace.config_path.read_text(encoding="utf-8")
    assert "staticGateway: nginx2" in raw2
    assert not any("nginx→builtin" in item for item in missing2)


def test_updater_migration_failure_keeps_original(workspace, monkeypatch) -> None:
    """067.c：备份失败（目录只读）→ 中止写回，原文件保持 nginx。"""
    workspace.config_path.write_text(
        "managerPort: 17800\nstaticGateway: nginx\n", encoding="utf-8"
    )
    cfg = load_config(workspace)
    ws_root = workspace.config_path.parent
    mode = ws_root.stat().st_mode
    try:
        os.chmod(ws_root, 0o500)
        _missing, written = migrate_config_defaults(workspace, cfg)
        assert written is False
        assert "staticGateway: nginx" in workspace.config_path.read_text(encoding="utf-8")
    finally:
        os.chmod(ws_root, mode)
