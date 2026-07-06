"""config 模块测试（WBS-02）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access.config import (
    Config,
    PortPool,
    default_config,
    load_config,
)
from local_webpage_access.errors import ConfigError
from local_webpage_access.paths import Workspace


def test_default_config_values() -> None:
    cfg = default_config()
    assert cfg.managerPort == 17800
    assert cfg.portPool.start == 18000
    assert cfg.portPool.end == 19999
    assert cfg.staticGateway == "caddy"
    assert cfg.buildConcurrency == 1
    assert cfg.defaultResourceLimits.memory == "512m"


def test_port_pool_range() -> None:
    pp = PortPool(start=20000, end=20010)
    assert list(pp.as_range())[0] == 20000
    assert list(pp.as_range())[-1] == 20010
    assert len(pp) == 11


def test_port_pool_invalid_range() -> None:
    with pytest.raises(ValueError):
        PortPool(start=200, end=100)


def test_port_pool_too_small() -> None:
    with pytest.raises(ValueError):
        PortPool(start=18000, end=18003)


def test_manager_port_in_pool_rejected() -> None:
    with pytest.raises(ValueError):
        Config(managerPort=18050)


def test_invalid_static_gateway() -> None:
    with pytest.raises(ValueError):
        Config(staticGateway="apache")


def test_manual_lan_requires_ip() -> None:
    with pytest.raises(ValueError):
        Config(lanIpStrategy="manual", manualLanIp=None)
    cfg = Config(lanIpStrategy="manual", manualLanIp="192.168.1.20")
    assert cfg.manualLanIp == "192.168.1.20"


def test_from_file(workspace: Workspace) -> None:
    workspace.config_path.write_text(
        "managerPort: 17800\nportPool:\n  start: 19000\n  end: 19100\nstaticGateway: nginx\n",
        encoding="utf-8",
    )
    cfg = load_config(workspace)
    assert cfg.portPool.start == 19000
    assert cfg.staticGateway == "nginx"


def test_load_config_missing_file_returns_default(workspace: Workspace) -> None:
    cfg = load_config(workspace)
    assert cfg.managerPort == 17800
    assert cfg.portPool.start == 18000


def test_load_config_invalid_yaml(workspace: Workspace) -> None:
    workspace.config_path.write_text("managerPort: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(workspace)


def test_load_config_not_a_mapping(workspace: Workspace) -> None:
    workspace.config_path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(workspace)


def test_save_and_reload_roundtrip(workspace: Workspace) -> None:
    cfg = Config(portPool=PortPool(start=19000, end=19200), buildConcurrency=2)
    cfg.save(workspace.config_path)
    loaded = load_config(workspace)
    assert loaded.portPool.start == 19000
    assert loaded.buildConcurrency == 2


def test_to_yaml_is_valid_yaml(workspace: Workspace) -> None:
    import yaml

    cfg = default_config()
    parsed = yaml.safe_load(cfg.to_yaml())
    assert isinstance(parsed, dict)
    assert parsed["managerPort"] == 17800
