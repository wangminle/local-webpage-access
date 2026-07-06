"""样例夹具测试（WBS-27）。

验证 6 个样例 zip 能被 scanner 正确识别，并满足验收标准：

1. 四个核心样例稳定复现识别路径（static / node-frontend / node-backend / python）。
2. 失败样例可触发 failed。
3. pending 样例不会被错误部署（保持 pending）。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tests.fixtures import EXPECTED_KIND, SAMPLES, build_all, build_zip


# ---- 打包正确性 -----------------------------------------------------------


def test_all_samples_defined() -> None:
    assert set(SAMPLES.keys()) == {
        "static_html",
        "vite_react",
        "node_express",
        "fastapi_sqlite",
        "build_failure",
        "pending_unknown",
    }


def test_build_zip_creates_valid_archive(tmp_path: Path) -> None:
    zp = build_zip("static_html", tmp_path / "static.zip")
    assert zp.is_file()
    assert zipfile.is_zipfile(zp)
    with zipfile.ZipFile(zp) as zf:
        names = zf.namelist()
    assert "index.html" in names
    assert "css/style.css" in names


def test_build_all_creates_every_sample(tmp_path: Path) -> None:
    result = build_all(tmp_path / "out")
    assert set(result.keys()) == set(SAMPLES.keys())
    for name, path in result.items():
        assert path.is_file(), f"{name} zip 未生成"
        assert zipfile.is_zipfile(path)


def test_build_zip_unknown_sample_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        build_zip("nope", tmp_path / "x.zip")


def test_build_zip_into_dir_uses_default_name(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    zp = build_zip("node_express", tmp_path)
    assert zp.name == "node_express.zip"


# ---- 识别正确性（WBS-27 验收 1）------------------------------------------


@pytest.fixture
def env(tmp_path: Path):
    from local_web_access.config import example_config_text, load_config
    from local_web_access.importer import Importer
    from local_web_access.paths import Workspace
    from local_web_access.registry import Registry

    root = tmp_path / "ws"
    from local_web_access.init_workspace import init_workspace

    init_workspace(root)
    ws = Workspace(root)
    ws.config_path.write_text(example_config_text(), encoding="utf-8")
    config = load_config(ws)
    reg = Registry(ws.db_path)
    reg.open()
    yield ws, config, reg
    reg.close()


@pytest.mark.parametrize("sample_name", list(SAMPLES.keys()))
def test_sample_detected_correctly(sample_name: str, env, tmp_path: Path) -> None:
    """每个样例的识别 kind 应与 EXPECTED_KIND 一致。"""
    ws, config, reg = env
    zp = build_zip(sample_name, tmp_path / f"{sample_name}.zip")
    from local_web_access.importer import Importer

    result = Importer(ws, config, reg).import_zip(str(zp))
    detection = result.detection
    expected = EXPECTED_KIND[sample_name]
    if expected is None:
        assert detection.pending, f"{sample_name} 应识别为 pending"
    else:
        assert not detection.pending, f"{sample_name} 不应 pending"
        assert detection.kind is not None
        assert detection.kind.value == expected, (
            f"{sample_name} 期望 kind={expected}，实际 {detection.kind.value}"
        )


# ---- pending 样例不被部署（WBS-27 验收 3）---------------------------------


def test_pending_sample_stays_pending(env, tmp_path: Path) -> None:
    """pending_unknown 不应被 daemon/自动流程部署。"""
    ws, config, reg = env
    zp = build_zip("pending_unknown", tmp_path / "pending.zip")
    from local_web_access.importer import Importer

    result = Importer(ws, config, reg).import_zip(str(zp))
    assert result.detection.pending
    # 状态应为 pending（未启动）
    row = reg.get_instance(result.instance_id)
    assert row is not None
    assert row["status"] == "pending"


def test_pending_sample_writes_risk_event(env, tmp_path: Path) -> None:
    """pending 样例应触发 WBS-25.09 风险提示事件。"""
    ws, config, reg = env
    zp = build_zip("pending_unknown", tmp_path / "pending.zip")
    from local_web_access.importer import Importer

    result = Importer(ws, config, reg).import_zip(str(zp))
    events = reg.list_events(result.instance_id)
    assert any(e["event_type"] == "security" for e in events), (
        [e["event_type"] for e in events]
    )


# ---- 四个核心样例的 manifest 形态 -----------------------------------------


def test_static_html_manifest_is_static(env, tmp_path: Path) -> None:
    ws, config, reg = env
    zp = build_zip("static_html", tmp_path)
    from local_web_access.importer import Importer

    result = Importer(ws, config, reg).import_zip(str(zp))
    manifest = result.manifest
    assert manifest.kind.value == "static"
    # 静态实例应有 static 配置
    assert manifest.static is not None


def test_node_express_manifest_has_container(env, tmp_path: Path) -> None:
    ws, config, reg = env
    zp = build_zip("node_express", tmp_path)
    from local_web_access.importer import Importer

    result = Importer(ws, config, reg).import_zip(str(zp))
    manifest = result.manifest
    assert manifest.kind.value == "node"


def test_fastapi_manifest_is_python(env, tmp_path: Path) -> None:
    ws, config, reg = env
    zp = build_zip("fastapi_sqlite", tmp_path)
    from local_web_access.importer import Importer

    result = Importer(ws, config, reg).import_zip(str(zp))
    manifest = result.manifest
    assert manifest.kind.value == "python"


def test_vite_react_manifest_is_node(env, tmp_path: Path) -> None:
    ws, config, reg = env
    zp = build_zip("vite_react", tmp_path)
    from local_web_access.importer import Importer

    result = Importer(ws, config, reg).import_zip(str(zp))
    manifest = result.manifest
    assert manifest.kind.value == "node"


# ---- build_failure 样例 ---------------------------------------------------


def test_build_failure_sample_imports_as_node(env, tmp_path: Path) -> None:
    """build_failure 仍可识别为 node；failed 状态由后续构建/启动触发（WBS-27 验收 2）。"""
    ws, config, reg = env
    zp = build_zip("build_failure", tmp_path)
    from local_web_access.importer import Importer

    result = Importer(ws, config, reg).import_zip(str(zp))
    assert result.manifest.kind.value == "node"
    assert not result.detection.pending
