"""models 模块测试（WBS-04）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_webpage_access.errors import SchemaError
from local_webpage_access.models import (
    SCHEMA_VERSION,
    ContainerConfig,
    DesiredState,
    InstanceManifest,
    Kind,
    NetworkConfig,
    ResourceProfile,
    Runtime,
    ServingMode,
    Status,
    StaticConfig,
)


def _static_manifest_dict() -> dict:
    return {
        "id": "my-demo",
        "name": "My Demo",
        "version": "2026.07.04-1",
        "kind": "node",
        "stack": ["vite", "react"],
        "runtime": "shared-static",
        "servingMode": "shared-static",
        "resourceProfile": "tiny",
        "network": {"hostPort": 18023, "lanUrl": "http://192.168.1.20:18023"},
    }


def test_static_manifest_valid() -> None:
    m = InstanceManifest.from_dict(_static_manifest_dict())
    assert m.kind == Kind.NODE
    assert m.runtime == Runtime.SHARED_STATIC
    assert m.servingMode == ServingMode.SHARED_STATIC
    assert m.resourceProfile == ResourceProfile.TINY
    assert m.status == Status.PENDING


def test_container_manifest_valid() -> None:
    data = {
        "id": "my-api",
        "name": "My API",
        "version": "2026.07.04-1",
        "kind": "python",
        "runtime": "docker-compose",
        "servingMode": "container",
        "container": {
            "projectName": "lwa-my-api",
            "internalPort": 8000,
            "composePath": "docker/compose.yaml",
            "dockerfilePath": "docker/Dockerfile",
        },
    }
    m = InstanceManifest.from_dict(data)
    assert m.container is not None
    assert m.container.projectName == "lwa-my-api"
    assert m.container.serviceName == "app"
    assert m.container.resourceLimits.memory == "512m"


def test_runtime_serving_mismatch_rejected() -> None:
    data = _static_manifest_dict()
    data["servingMode"] = "container"
    with pytest.raises(SchemaError):
        InstanceManifest.from_dict(data)


def test_docker_compose_without_container_rejected() -> None:
    data = {
        "id": "x",
        "name": "X",
        "version": "1",
        "kind": "node",
        "runtime": "docker-compose",
        "servingMode": "container",
    }
    with pytest.raises(SchemaError):
        InstanceManifest.from_dict(data)


def test_shared_static_with_container_rejected() -> None:
    data = _static_manifest_dict()
    data["container"] = {
        "projectName": "x",
        "internalPort": 8000,
        "composePath": "docker/compose.yaml",
        "dockerfilePath": "docker/Dockerfile",
    }
    with pytest.raises(SchemaError):
        InstanceManifest.from_dict(data)


def test_database_consistency() -> None:
    data = _static_manifest_dict()
    data["hasDatabase"] = True
    with pytest.raises(SchemaError):
        InstanceManifest.from_dict(data)
    data2 = _static_manifest_dict()
    data2["hasDatabase"] = True
    data2["database"] = {"type": "sqlite", "dataDir": "data"}
    m = InstanceManifest.from_dict(data2)
    assert m.database is not None
    assert m.database.type == "sqlite"


def test_missing_required_fields() -> None:
    with pytest.raises(SchemaError):
        InstanceManifest.from_dict({"id": "x"})


def test_invalid_kind() -> None:
    data = _static_manifest_dict()
    data["kind"] = "ruby"
    with pytest.raises(SchemaError):
        InstanceManifest.from_dict(data)


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    m = InstanceManifest.from_dict(_static_manifest_dict())
    path = tmp_path / "local-web.json"
    m.save(path)
    loaded = InstanceManifest.load(path)
    assert loaded.id == "my-demo"
    assert loaded.network.hostPort == 18023


def test_load_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "local-web.json"
    path.write_text("{bad json", encoding="utf-8")
    with pytest.raises(SchemaError):
        InstanceManifest.load(path)


def test_default_status_pending() -> None:
    m = InstanceManifest.from_dict(_static_manifest_dict())
    assert m.status == Status.PENDING
    assert m.desiredState == DesiredState.STOPPED


def test_schema_version_is_one() -> None:
    assert SCHEMA_VERSION == 1


def test_to_dict_serializable_to_json() -> None:
    m = InstanceManifest.from_dict(_static_manifest_dict())
    data = m.to_dict()
    # 必须可被 json.dumps 序列化
    s = json.dumps(data, ensure_ascii=False)
    assert "my-demo" in s
    # 枚举应已转为字符串值
    assert data["kind"] == "node"


def test_touch_updates_timestamp() -> None:
    import time

    m = InstanceManifest.from_dict(_static_manifest_dict())
    old = m.updatedAt
    time.sleep(1.1)
    m.touch()
    assert m.updatedAt > old


def test_extra_fields_allowed() -> None:
    """允许额外字段，为 schema 演进预留空间。"""
    data = _static_manifest_dict()
    data["futureField"] = {"anything": True}
    m = InstanceManifest.from_dict(data)
    # 额外字段保留在 model_dump 中
    dumped = m.to_dict()
    assert "futureField" in dumped
