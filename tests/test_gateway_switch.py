"""``gateway_switch`` 单测（IMP-037 / DEV-082，WBS 037.01）。

覆盖：caddy↔builtin 双向切换、幂等 noop、缺 Caddy 预检失败、中途失败回滚、
dry-run 不写盘。StaticGateway / start_gateway / stop_gateway / access 均用替身。
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from local_webpage_access.config import Config, PortPool
from local_webpage_access.errors import LwaError
from local_webpage_access.models import (
    DesiredState,
    RouteMode,
    StaticConfig,
    Status,
)
from local_webpage_access.paths import Workspace
from tests._helpers import make_static_manifest


def _caddy_config() -> Config:
    return Config(
        staticGateway="caddy",
        staticGatewayPort=18080,
        portPool=PortPool(start=21000, end=21050),
    )


def _builtin_config() -> Config:
    return Config(
        staticGateway="builtin",
        staticGatewayPort=18080,
        portPool=PortPool(start=21000, end=21050),
    )


def _seed_static(
    workspace: Workspace,
    registry,
    *,
    iid: str = "demo",
    gateway: str = "caddy",
    host_port: int = 21001,
    route_host: str | None = "demo-alias",
    status: Status = Status.RUNNING,
    desired: DesiredState = DesiredState.RUNNING,
) -> None:
    static_kw: dict = {
        "hostPort": host_port,
        "gateway": gateway,
        "enabled": True,
        "root": "public",
    }
    if route_host is not None:
        static_kw["routeMode"] = RouteMode.NAME.value
        static_kw["routeHost"] = route_host
    else:
        static_kw["routeMode"] = RouteMode.PORT.value
    manifest = make_static_manifest(
        iid,
        static=StaticConfig(**static_kw),
        status=status,
        desiredState=desired,
    )
    app_dir = workspace.app_dir(iid)
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "current").mkdir(parents=True, exist_ok=True)
    public = workspace.app_public(iid)
    public.mkdir(parents=True, exist_ok=True)
    (public / "index.html").write_text("<html>ok</html>\n", encoding="utf-8")
    manifest.appPath = str(app_dir / "current")
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    # 别名片段（切 builtin 时应保留；切回 caddy 应重建）
    if route_host is not None:
        alias_path = workspace.app_alias_config(iid)
        alias_path.parent.mkdir(parents=True, exist_ok=True)
        alias_path.write_text(
            f"# alias {route_host} -> {host_port}\n",
            encoding="utf-8",
        )
    site_path = workspace.static_gateway / "sites" / f"{iid}.conf"
    site_path.parent.mkdir(parents=True, exist_ok=True)
    site_path.write_text(f"# site {iid} :{host_port}\n", encoding="utf-8")


@pytest.fixture()
def switch_fakes(monkeypatch, workspace: Workspace):
    """可控替身：StaticGateway / start_gateway / stop_gateway / run_access_pass。"""
    state = {
        "backend": "caddy",
        "admin_alive": True,
        "start_ok": True,
        "stop_ok": True,
        "enable_ok": True,
        "enable_calls": [],
        "generate_alias_calls": [],
        "reload_calls": 0,
        "stop_builtin_calls": 0,
        "sync_calls": 0,
        "start_gateway_calls": 0,
        "stop_gateway_calls": 0,
        "access_calls": 0,
        "access_review_fail": False,
        "fail_after_stop": False,
        "call_order": [],
        "pid": 4242,
    }

    class _FakeGW:
        def __init__(self, ws: Workspace, cfg: Config) -> None:
            self.ws = ws
            self.cfg = cfg

        def detect_backend(self) -> str:
            return state["backend"]

        def _admin_alive(self, **kw) -> bool:
            return state["admin_alive"]

        def enable(self, instance_id, host_port, root, *, wait_health=True, alias=None):
            state["enable_calls"].append(
                {
                    "id": instance_id,
                    "host_port": host_port,
                    "alias": alias,
                    "backend": self.cfg.staticGateway,
                }
            )
            state["call_order"].append(f"enable:{instance_id}")
            if not state["enable_ok"]:
                raise LwaError("enable failed", code="GATEWAY_ENABLE_FAIL")

        def generate_alias_config(self, instance_id, alias, host_port):
            state["generate_alias_calls"].append(
                {"id": instance_id, "alias": alias, "host_port": host_port}
            )
            path = self.ws.app_alias_config(instance_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"# regenerated {alias} -> {host_port}\n", encoding="utf-8"
            )
            return path

        def reload_all(self) -> None:
            state["reload_calls"] += 1
            state["call_order"].append("reload_all")

        def stop_all_builtin(self) -> list[str]:
            state["stop_builtin_calls"] += 1
            state["call_order"].append("stop_all_builtin")
            return []

        def _sync_main_config(self) -> None:
            state["sync_calls"] += 1
            state["call_order"].append("sync_main")

        def main_config_path(self) -> Path:
            return self.ws.static_gateway / "Caddyfile"

        def caddy_pid_path(self) -> Path:
            return self.ws.run / "caddy.pid"

    def _fake_start(ws, cfg, *, registry=None):
        state["start_gateway_calls"] += 1
        state["call_order"].append("start_gateway")
        if not state["start_ok"]:
            raise LwaError("caddy start failed", code="GATEWAY_START_FAIL")
        state["backend"] = "caddy"
        state["admin_alive"] = True
        ws.run.mkdir(parents=True, exist_ok=True)
        (ws.run / "caddy.pid").write_text(str(state["pid"]))
        from local_webpage_access.gateway_service import GatewayState, write_state

        write_state(
            ws,
            GatewayState(
                enabled=True, pid=state["pid"], started_at="t", port=cfg.staticGatewayPort
            ),
        )
        return state["pid"]

    def _fake_stop(ws, cfg):
        state["stop_gateway_calls"] += 1
        state["call_order"].append("stop_gateway")
        if state["fail_after_stop"]:
            # 停成功后下一阶段（写配置）由测试注入；此处仅记录 stop
            pass
        if not state["stop_ok"]:
            return False
        state["admin_alive"] = False
        with contextlib.suppress(FileNotFoundError):
            (ws.run / "caddy.pid").unlink()
        from local_webpage_access.gateway_service import read_state, write_state

        st = read_state(ws)
        if st is not None:
            st.enabled = False
            st.pid = None
            write_state(ws, st)
        return True

    def _fake_access(ws, cfg, reg, *, review=True, dry_run=False):
        from local_webpage_access.access_workflow import AccessPassResult

        state["access_calls"] += 1
        state["call_order"].append("access_pass")
        result = AccessPassResult()
        if state["access_review_fail"]:
            result.review_error = "review boom"
        return result

    monkeypatch.setattr(
        "local_webpage_access.gateway_switch.StaticGateway", _FakeGW
    )
    monkeypatch.setattr(
        "local_webpage_access.gateway_switch.start_gateway", _fake_start
    )
    monkeypatch.setattr(
        "local_webpage_access.gateway_switch.stop_gateway", _fake_stop
    )
    monkeypatch.setattr(
        "local_webpage_access.gateway_switch.run_access_pass", _fake_access
    )
    # 切到 caddy 时预检；切到 builtin 不要求
    monkeypatch.setattr(
        "local_webpage_access.gateway_switch._caddy_available",
        lambda: state.get("caddy_available", True),
    )
    return state


def test_switch_caddy_to_builtin_updates_manifest_keeps_alias_meta(
    workspace: Workspace, registry, switch_fakes
) -> None:
    from local_webpage_access.gateway_switch import switch_gateway

    cfg = _caddy_config()
    cfg.save(workspace.config_path)
    _seed_static(workspace, registry, gateway="caddy")
    switch_fakes["backend"] = "caddy"

    result = switch_gateway(workspace, cfg, registry, "builtin", review=True)

    assert result.ok is True
    assert result.noop is False
    assert result.from_backend == "caddy"
    assert result.to_backend == "builtin"
    assert switch_fakes["stop_gateway_calls"] == 1
    assert switch_fakes["start_gateway_calls"] == 0
    assert len(switch_fakes["enable_calls"]) == 1
    assert switch_fakes["enable_calls"][0]["backend"] == "builtin"
    assert switch_fakes["access_calls"] == 1

    from local_webpage_access.config import load_config
    from local_webpage_access.models import InstanceManifest

    reloaded = load_config(workspace)
    assert reloaded.staticGateway == "builtin"
    m = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert m.static is not None
    assert m.static.gateway == "builtin"
    assert m.static.routeHost == "demo-alias"  # 元数据保留
    row = registry.get_static_site("demo")
    assert row is not None
    assert row["gateway"] == "builtin"
    assert row["route_host"] == "demo-alias"
    # 别名片段文件仍在（不删除）
    assert workspace.app_alias_config("demo").is_file()


def test_switch_builtin_to_caddy_rebuilds_alias(
    workspace: Workspace, registry, switch_fakes
) -> None:
    from local_webpage_access.gateway_switch import switch_gateway

    cfg = _builtin_config()
    cfg.save(workspace.config_path)
    _seed_static(workspace, registry, gateway="builtin")
    switch_fakes["backend"] = "builtin"
    switch_fakes["admin_alive"] = False

    result = switch_gateway(workspace, cfg, registry, "caddy", review=False)

    assert result.ok is True
    assert result.from_backend == "builtin"
    assert result.to_backend == "caddy"
    assert switch_fakes["start_gateway_calls"] == 1
    assert switch_fakes["generate_alias_calls"]
    assert switch_fakes["generate_alias_calls"][0]["alias"] == "demo-alias"
    assert "regenerated demo-alias" in workspace.app_alias_config("demo").read_text(
        encoding="utf-8"
    )

    from local_webpage_access.config import load_config
    from local_webpage_access.models import InstanceManifest

    assert load_config(workspace).staticGateway == "caddy"
    m = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert m.static is not None and m.static.gateway == "caddy"
    row = registry.get_static_site("demo")
    assert row is not None and row["gateway"] == "caddy"


def test_switch_noop_when_already_on_target(
    workspace: Workspace, registry, switch_fakes
) -> None:
    from local_webpage_access.gateway_switch import switch_gateway

    cfg = _builtin_config()
    cfg.save(workspace.config_path)
    _seed_static(workspace, registry, gateway="builtin")
    yml_before = workspace.config_path.read_text(encoding="utf-8")

    result = switch_gateway(workspace, cfg, registry, "builtin")

    assert result.ok is True
    assert result.noop is True
    assert switch_fakes["stop_gateway_calls"] == 0
    assert switch_fakes["start_gateway_calls"] == 0
    assert switch_fakes["enable_calls"] == []
    assert workspace.config_path.read_text(encoding="utf-8") == yml_before


def test_switch_to_caddy_fails_when_caddy_missing(
    workspace: Workspace, registry, switch_fakes
) -> None:
    from local_webpage_access.gateway_switch import switch_gateway

    cfg = _builtin_config()
    cfg.save(workspace.config_path)
    _seed_static(workspace, registry, gateway="builtin")
    switch_fakes["caddy_available"] = False
    yml_before = workspace.config_path.read_text(encoding="utf-8")

    result = switch_gateway(workspace, cfg, registry, "caddy")

    assert result.ok is False
    assert result.error
    assert "caddy" in (result.error or "").lower() or "Caddy" in (result.error or "")
    assert switch_fakes["start_gateway_calls"] == 0
    assert workspace.config_path.read_text(encoding="utf-8") == yml_before


def test_switch_mid_fail_rolls_back_yaml(
    workspace: Workspace, registry, switch_fakes
) -> None:
    from local_webpage_access.gateway_switch import switch_gateway

    cfg = _caddy_config()
    cfg.save(workspace.config_path)
    _seed_static(workspace, registry, gateway="caddy")
    switch_fakes["backend"] = "caddy"
    # stop 成功后 enable 失败 → 应回滚 YAML 到 caddy
    switch_fakes["enable_ok"] = False

    result = switch_gateway(workspace, cfg, registry, "builtin")

    assert result.ok is False
    assert result.degraded is False or result.repair_hint is not None
    from local_webpage_access.config import load_config

    assert load_config(workspace).staticGateway == "caddy"
    # 回滚应尝试重启旧后端（caddy）
    assert switch_fakes["start_gateway_calls"] >= 1


def test_dry_run_does_not_write(
    workspace: Workspace, registry, switch_fakes
) -> None:
    from local_webpage_access.gateway_switch import plan_switch, switch_gateway

    cfg = _caddy_config()
    cfg.save(workspace.config_path)
    _seed_static(workspace, registry, gateway="caddy")
    yml_before = workspace.config_path.read_text(encoding="utf-8")
    alias_before = workspace.app_alias_config("demo").read_text(encoding="utf-8")

    plan = plan_switch(workspace, cfg, registry, "builtin")
    assert plan.from_backend == "caddy"
    assert plan.to_backend == "builtin"
    assert any(i.get("id") == "demo" for i in plan.instances)

    result = switch_gateway(workspace, cfg, registry, "builtin", dry_run=True)
    assert result.ok is True
    assert result.noop is False
    assert switch_fakes["stop_gateway_calls"] == 0
    assert switch_fakes["enable_calls"] == []
    assert workspace.config_path.read_text(encoding="utf-8") == yml_before
    assert workspace.app_alias_config("demo").read_text(encoding="utf-8") == alias_before
    assert not (workspace.config_path.with_suffix(".yml.bak")).exists()


def test_review_failure_keeps_backend_ok_but_access_not_fully_ok(
    workspace: Workspace, registry, switch_fakes
) -> None:
    from local_webpage_access.gateway_switch import switch_gateway

    cfg = _caddy_config()
    cfg.save(workspace.config_path)
    _seed_static(workspace, registry, gateway="caddy")
    switch_fakes["access_review_fail"] = True

    result = switch_gateway(workspace, cfg, registry, "builtin", review=True)

    assert result.ok is True  # 后端切换成功
    assert result.access is not None
    assert result.access_ok is False
    assert result.fully_ok is False


def test_plan_switch_rejects_invalid_target(
    workspace: Workspace, registry, switch_fakes
) -> None:
    from local_webpage_access.gateway_switch import plan_switch

    cfg = _builtin_config()
    with pytest.raises(LwaError) as ei:
        plan_switch(workspace, cfg, registry, "nginx")
    assert "GATEWAY" in ei.value.code or "backend" in str(ei.value).lower() or "nginx" in str(
        ei.value
    ).lower() or "目标" in str(ei.value)


def test_cli_switch_dry_run_precheck_failure_shows_error(
    workspace: Workspace, registry, monkeypatch
) -> None:
    """``lwa gateway switch caddy --dry-run`` 预检失败时打印错误而非误导性 dry-run 预览。

    回归：dry-run 分支曾在 ``not result.ok`` 之前，预检失败（本机无 caddy）时只
    打印 ``[dry-run] builtin -> caddy`` 后 exit 1，吞掉错误信息。
    """
    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    from local_webpage_access.gateway_switch import GatewaySwitchResult

    cfg = _builtin_config()
    cfg.save(workspace.config_path)

    def _fake_open():
        return workspace, cfg, registry

    def _fake_switch(ws, config, reg, target, *, dry_run=False, review=True):
        # 模拟 plan_switch 预检失败（切到 caddy 但本机无 caddy）
        return GatewaySwitchResult(
            ok=False,
            from_backend="builtin",
            to_backend="caddy",
            error="未找到 caddy 可执行文件，无法切换到 caddy 后端",
            repair_hint="安装 Caddy 并加入 PATH，或改用 lwa gateway switch builtin",
        )

    monkeypatch.setattr(
        "local_webpage_access.cli.gateway.open_workspace_registry", _fake_open
    )
    monkeypatch.setattr(
        "local_webpage_access.gateway_switch.switch_gateway", _fake_switch
    )

    res = CliRunner().invoke(app, ["gateway", "switch", "caddy", "--dry-run"])
    assert res.exit_code == 1
    assert "网关切换失败" in res.output
    assert "caddy" in res.output.lower()
    # 不应打印误导性的 dry-run 成功预览
    assert "[dry-run] builtin -> caddy" not in res.output
