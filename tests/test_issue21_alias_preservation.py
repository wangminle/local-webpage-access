"""issue #21 回归：liveness/活验证失败回滚不得丢失容器路径别名。

事故链路（2026-08-30，实例 prd-review-v0.35，V0.8.7）：
``lwa import --update`` → rebuild 探针失败 → 后续 start 的
``maybe_verify_alias_after_start`` 活验证失败（应用慢启动 / 入口临时 5xx /
根绝对资源）→ 旧代码把**已验证过**的别名当 deferred 别名清除
（``_rollback_deferred_alias_after_failed_live_verify``）：
``routeMode`` 重置 port、``routeHost`` 清空、别名片段删除、主 Caddyfile
不再 import——入口静默失效，且网关对未命中前缀返回 200 空体难以定位。

修复口径：
1. 已验证别名（``aliasLiveVerifiedFor`` 标记）或片段在本轮 start 前已在盘
   （存量实例旁证）→ 活验证失败仅警告 + 事件，保留别名与片段，不抛错；
2. deferred 别名（从未验证、无标记、片段系本轮生成）→ 维持 BUG-586
   清除行为（导入期设置的坏别名不得残留）；
3. 统一入口块加 ``respond 404`` 兜底，未命中别名路由不再返回空 200。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from local_webpage_access.config import Config
from local_webpage_access.errors import RecognitionError
from local_webpage_access.importer import Importer
from local_webpage_access.models import (
    DesiredState,
    InstanceManifest,
    Status,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.static_gateway import StaticGateway


# ---- fixtures --------------------------------------------------------------


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    w = Workspace(tmp_path / "ws")
    w.ensure_workspace_dirs()
    return w


@pytest.fixture()
def cfg() -> Config:
    from local_webpage_access.config import PortPool

    return Config(staticGateway="builtin", portPool=PortPool(start=21000, end=21050))


@pytest.fixture()
def reg(ws: Workspace) -> Registry:
    ws.root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    r = Registry(ws.root / "registry" / "local-web.db")
    r.open()
    yield r
    r.close()


@pytest.fixture()
def container_instance(
    ws: Workspace, cfg: Config, reg: Registry, tmp_path: Path
) -> tuple[str, Path]:
    """导入一个带别名（routeMode=name）的容器实例，返回 (iid, manifest_path)。"""
    zp = tmp_path / "api.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("requirements.txt", "fastapi\nuvicorn\n")
        zf.writestr("main.py", "app=1\n")
    importer = Importer(ws, cfg, reg)
    result = importer.import_zip(zp)
    iid = result.instance_id
    mpath = ws.app_manifest_path(iid)
    manifest = InstanceManifest.load(mpath)
    assert manifest.container is not None
    manifest.container.routeMode = "name"
    manifest.container.routeHost = "prd-review"
    manifest.container.hostPort = 18004
    manifest.status = Status.RUNNING
    manifest.desiredState = DesiredState.RUNNING
    manifest.save(mpath)
    reg.upsert_from_manifest(manifest)
    reg.update_status(iid, Status.RUNNING.value, desired_state=DesiredState.RUNNING.value)
    # 别名片段在盘（历史部署周期生成）
    alias_conf = ws.app_alias_config(iid)
    alias_conf.parent.mkdir(parents=True, exist_ok=True)
    alias_conf.write_text(
        "handle_path /prd-review/* {\n\treverse_proxy 127.0.0.1:18004\n}\n",
        encoding="utf-8",
    )
    return iid, mpath


class _CaddyFakeGW:
    """Caddy 替身：detect_backend=caddy，reload 空操作。"""

    def __init__(self, workspace, config) -> None:
        self.ws = workspace

    def detect_backend(self) -> str:
        return "caddy"

    def is_enabled(self, iid) -> bool:
        return True

    def generate_alias_config(self, iid, alias, hp, **kwargs) -> None:
        p = self.ws.app_alias_config(iid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# alias {alias}\n", encoding="utf-8")

    def reload_all(self) -> None:
        return None

    def remove_alias_config(self, iid) -> None:
        p = self.ws.app_alias_config(iid)
        if p.is_file():
            p.unlink()


def _fail_verify(monkeypatch, message: str = "别名入口活验证失败（HTTP 503）") -> None:
    from local_webpage_access import path_alias as pa

    def boom(*args, **kwargs):
        raise RecognitionError(message)

    monkeypatch.setattr(pa, "verify_alias_live", boom)
    monkeypatch.setattr(pa, "StaticGateway", _CaddyFakeGW)
    monkeypatch.setattr(pa, "_fetch_entrypoint_html_for_alias_guard", lambda **kw: None)


# ---- 已验证别名：活验证失败保留 --------------------------------------------


def test_verified_alias_survives_live_verify_failure(
    ws, cfg, reg, container_instance, monkeypatch
) -> None:
    """issue #21：带验证标记的别名，启动后活验证失败不得清除。"""
    from local_webpage_access import path_alias as pa

    iid, mpath = container_instance
    manifest = InstanceManifest.load(mpath)
    manifest.aliasLiveVerifiedAt = "2026-08-30T00:00:00Z"
    manifest.aliasLiveVerifiedFor = "prd-review"
    manifest.save(mpath)

    _fail_verify(monkeypatch)

    # 不抛错（应用本身在跑，别名是用户资产）
    result = pa.maybe_verify_alias_after_start(ws, cfg, reg, iid, manifest)
    assert result is False

    reloaded = InstanceManifest.load(mpath)
    assert reloaded.container is not None
    assert reloaded.container.routeMode == "name"
    assert reloaded.container.routeHost == "prd-review"
    assert ws.app_alias_config(iid).is_file()
    # registry 不被清
    row = reg.get_container(iid)
    assert row is not None and row.get("route_host") == "prd-review"
    # 事件透出失败事实
    events = [e for e in reg.list_events(iid, limit=20) if e.get("event_type") == "path-alias"]
    assert any("已保留别名配置" in (e.get("message") or "") for e in events)


def test_legacy_alias_fragment_preexisting_survives_verify_failure(
    ws, cfg, reg, container_instance, monkeypatch
) -> None:
    """issue #21：存量实例（无标记、片段本轮前已在盘）同样保留。"""
    from local_webpage_access import path_alias as pa

    iid, mpath = container_instance
    manifest = InstanceManifest.load(mpath)
    assert manifest.aliasLiveVerifiedAt is None  # 存量无标记

    _fail_verify(monkeypatch)

    result = pa.maybe_verify_alias_after_start(
        ws, cfg, reg, iid, manifest, alias_fragment_preexisting=True
    )
    assert result is False

    reloaded = InstanceManifest.load(mpath)
    assert reloaded.container is not None
    assert reloaded.container.routeMode == "name"
    assert reloaded.container.routeHost == "prd-review"
    assert ws.app_alias_config(iid).is_file()


def test_verified_alias_survives_verify_exception(
    ws, cfg, reg, container_instance, monkeypatch
) -> None:
    """issue #21：活验证抛非 RecognitionError 异常（如连接拒绝）同样保留。"""
    from local_webpage_access import path_alias as pa

    iid, mpath = container_instance
    manifest = InstanceManifest.load(mpath)
    manifest.aliasLiveVerifiedAt = "2026-08-30T00:00:00Z"
    manifest.aliasLiveVerifiedFor = "prd-review"
    manifest.save(mpath)

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(pa, "verify_alias_live", boom)
    monkeypatch.setattr(pa, "StaticGateway", _CaddyFakeGW)
    monkeypatch.setattr(pa, "_fetch_entrypoint_html_for_alias_guard", lambda **kw: None)

    result = pa.maybe_verify_alias_after_start(ws, cfg, reg, iid, manifest)
    assert result is False

    reloaded = InstanceManifest.load(mpath)
    assert reloaded.container is not None
    assert reloaded.container.routeHost == "prd-review"
    assert ws.app_alias_config(iid).is_file()


# ---- deferred 别名：维持 BUG-586 清除 --------------------------------------


def test_deferred_alias_without_marker_still_rolls_back(
    ws, cfg, reg, container_instance, monkeypatch
) -> None:
    """deferred 别名（无标记、片段非本轮前在盘）维持 BUG-586 清除行为。"""
    from local_webpage_access import path_alias as pa

    iid, mpath = container_instance
    manifest = InstanceManifest.load(mpath)
    assert manifest.aliasLiveVerifiedAt is None

    _fail_verify(monkeypatch)

    with pytest.raises(RecognitionError):
        pa.maybe_verify_alias_after_start(
            ws, cfg, reg, iid, manifest, alias_fragment_preexisting=False
        )

    reloaded = InstanceManifest.load(mpath)
    assert reloaded.container is not None
    assert reloaded.container.routeMode == "port"
    assert reloaded.container.routeHost is None
    assert not ws.app_alias_config(iid).is_file()


def test_marker_for_other_alias_does_not_protect(
    ws, cfg, reg, container_instance, monkeypatch
) -> None:
    """标记属于旧别名（换名后未再验证）不得保护当前别名。"""
    from local_webpage_access import path_alias as pa

    iid, mpath = container_instance
    manifest = InstanceManifest.load(mpath)
    manifest.aliasLiveVerifiedAt = "2026-08-30T00:00:00Z"
    manifest.aliasLiveVerifiedFor = "old-alias"  # ≠ prd-review
    manifest.save(mpath)

    _fail_verify(monkeypatch)

    with pytest.raises(RecognitionError):
        pa.maybe_verify_alias_after_start(
            ws, cfg, reg, iid, manifest, alias_fragment_preexisting=False
        )


# ---- 标记写入与清除 ---------------------------------------------------------


def test_verify_success_writes_marker(ws, cfg, reg, container_instance, monkeypatch) -> None:
    """活验证通过 → 落 aliasLiveVerifiedAt/For 标记。"""
    from local_webpage_access import path_alias as pa

    iid, mpath = container_instance
    manifest = InstanceManifest.load(mpath)

    monkeypatch.setattr(pa, "verify_alias_live", lambda *a, **k: None)
    monkeypatch.setattr(pa, "StaticGateway", _CaddyFakeGW)
    monkeypatch.setattr(pa, "_fetch_entrypoint_html_for_alias_guard", lambda **kw: None)

    assert pa.maybe_verify_alias_after_start(ws, cfg, reg, iid, manifest) is True

    reloaded = InstanceManifest.load(mpath)
    assert reloaded.aliasLiveVerifiedAt is not None
    assert reloaded.aliasLiveVerifiedFor == "prd-review"


def test_apply_manifest_alias_clears_stale_marker(ws, cfg, container_instance) -> None:
    """换别名/清别名时旧标记失效（_apply_manifest_alias）。"""
    from local_webpage_access.path_alias import _apply_manifest_alias

    _, mpath = container_instance
    manifest = InstanceManifest.load(mpath)
    manifest.aliasLiveVerifiedAt = "2026-08-30T00:00:00Z"
    manifest.aliasLiveVerifiedFor = "prd-review"

    _apply_manifest_alias(manifest, cfg, "new-alias")
    assert manifest.aliasLiveVerifiedAt is None
    assert manifest.aliasLiveVerifiedFor is None

    # 同名重设不清除标记
    manifest.aliasLiveVerifiedAt = "2026-08-30T00:00:00Z"
    manifest.aliasLiveVerifiedFor = "prd-review"
    _apply_manifest_alias(manifest, cfg, "prd-review")
    assert manifest.aliasLiveVerifiedAt == "2026-08-30T00:00:00Z"


# ---- 统一入口 404 兜底 ------------------------------------------------------


def test_main_config_alias_block_has_404_fallback(ws, cfg) -> None:
    """issue #21：统一入口块尾部有 respond 404 兜底 handle。"""
    gw = StaticGateway(ws, cfg)
    gw.generate_alias_config("demo", "voiceprint", 18001)

    content = gw._assemble_main_config()
    assert "\thandle {" in content
    assert "\t\trespond 404" in content
    # 兜底 handle 必须在别名 import 之后（Caddy handle 按出现顺序互斥执行）
    lines = content.splitlines()
    last_import_idx = max(
        i for i, ln in enumerate(lines) if ln.startswith("\timport ")
    )
    fallback_idx = next(i for i, ln in enumerate(lines) if ln.strip() == "respond 404")
    assert fallback_idx > last_import_idx


def test_main_config_no_fallback_without_alias_block(ws, cfg) -> None:
    """无别名片段时无统一入口块，也无 404 兜底（行为不变）。"""
    gw = StaticGateway(ws, cfg)
    content = gw._assemble_main_config()
    assert "respond 404" not in content
