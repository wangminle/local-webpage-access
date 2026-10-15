"""S3-06 顺手拆分的行为不变契约：host_container 阶段编排 + 历史类路由子模块。

真实部署/补偿路径由既有的 ``test_hosting`` / ``test_s2_03_rebuild_txn`` /
``test_bug746_756_stop_semantics`` 等用例与真实 Docker 用例覆盖；本文件只固定
拆分引入的新结构：阶段顺序、异常收尾分派、共享状态对象、路由归属与路径集合。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from local_webpage_access import hosting
from local_webpage_access.errors import HostingError


# ---------------------------------------------------------------------------
# host_container 阶段编排
# ---------------------------------------------------------------------------

_STAGES = (
    "_prepare_previous_container",
    "_render_container_artifacts",
    "_build_container_image",
    "_swap_in_new_container",
)
_POST_STAGES = (
    "_observe_new_container",
    "_commit_verifying_state",
    "_verify_container_deployment",
    "_finalize_container_deploy",
)


def _install_fakes(monkeypatch: pytest.MonkeyPatch, calls: list[str], *, fail_at: str | None,
                   exc: BaseException | None = None) -> object:
    manifest = SimpleNamespace(
        runtime=SimpleNamespace(value="docker-compose"), container=SimpleNamespace()
    )
    ctx = SimpleNamespace(tag="ctx", outcome_settled=False)
    sentinel_result = object()

    class _FakeRuntime:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        @staticmethod
        def ensure_available() -> None:
            calls.append("ensure_available")

    monkeypatch.setattr(hosting, "DockerRuntime", _FakeRuntime)
    monkeypatch.setattr(hosting, "_load_manifest", lambda *_a, **_k: manifest)
    monkeypatch.setattr(hosting, "_record_stage", lambda *_a, **_k: calls.append("record_stage"))
    monkeypatch.setattr(
        "local_webpage_access.container_identity.ensure_non_root_identity_ready",
        lambda *_a, **_k: calls.append("non_root_check"),
    )

    def begin(*_a: Any, **_k: Any) -> object:
        calls.append("_begin_container_deploy")
        return ctx

    monkeypatch.setattr(hosting, "_begin_container_deploy", begin)

    def make_stage(name: str):
        def stage(got_ctx: object, *rest: Any) -> Any:
            assert got_ctx is ctx, f"{name} 必须收到同一份共享状态"
            calls.append(name)
            if name == fail_at and exc is not None:
                raise exc
            if name == "_verify_container_deployment":
                return {"overall_status": "passed"}
            if name == "_finalize_container_deploy":
                return sentinel_result
            return None

        return stage

    for name in (*_STAGES, *_POST_STAGES):
        monkeypatch.setattr(hosting, name, make_stage(name))
    for name in ("_handle_container_deploy_cancelled", "_handle_container_deploy_failed"):
        monkeypatch.setattr(
            hosting, name, lambda got_ctx, e, _n=name: (calls.append(_n), calls.append(type(e).__name__))
        )
    return sentinel_result


def test_host_container_runs_stages_in_order_and_returns_finalized_manifest(monkeypatch) -> None:
    calls: list[str] = []
    result = _install_fakes(monkeypatch, calls, fail_at=None)
    out = hosting.host_container(object(), object(), object(), "demo")  # type: ignore[arg-type]
    assert out is result
    stage_calls = [c for c in calls if c.startswith("_") and c != "_begin_container_deploy"]
    assert stage_calls == [*_STAGES, *_POST_STAGES]
    # 非 root 预检先于任何阶段（先于数据救出/停机/生成器覆写）
    assert calls.index("non_root_check") < calls.index(_STAGES[0])
    assert calls.index("ensure_available") < calls.index("_begin_container_deploy")


@pytest.mark.parametrize("fail_stage", (*_STAGES, *_POST_STAGES))
def test_stage_failure_dispatches_failed_handler_then_reraises(monkeypatch, fail_stage) -> None:
    calls: list[str] = []
    boom = HostingError("boom", instance_id="demo")
    _install_fakes(monkeypatch, calls, fail_at=fail_stage, exc=boom)
    with pytest.raises(HostingError) as info:
        hosting.host_container(object(), object(), object(), "demo")  # type: ignore[arg-type]
    assert info.value is boom  # 收尾后原异常原样抛出
    assert "_handle_container_deploy_failed" in calls
    assert "_handle_container_deploy_cancelled" not in calls
    # 失败后不再进入后续阶段
    all_stages = (*_STAGES, *_POST_STAGES)
    later = all_stages[all_stages.index(fail_stage) + 1 :]
    assert not any(c in calls for c in later)


def test_build_cancelled_dispatches_cancelled_handler_only(monkeypatch) -> None:
    calls: list[str] = []
    cancelled = hosting.BuildCancelled("cancelled by user")
    _install_fakes(monkeypatch, calls, fail_at="_build_container_image", exc=cancelled)
    with pytest.raises(hosting.BuildCancelled):
        hosting.host_container(object(), object(), object(), "demo")  # type: ignore[arg-type]
    assert "_handle_container_deploy_cancelled" in calls
    assert "_handle_container_deploy_failed" not in calls


def test_non_container_instance_rejected_before_any_side_effect(monkeypatch) -> None:
    calls: list[str] = []
    _install_fakes(monkeypatch, calls, fail_at=None)
    monkeypatch.setattr(
        hosting,
        "_load_manifest",
        lambda *_a, **_k: SimpleNamespace(
            runtime=SimpleNamespace(value="static"), container=None
        ),
    )
    with pytest.raises(HostingError):
        hosting.host_container(object(), object(), object(), "demo")  # type: ignore[arg-type]
    assert calls == []  # 未检查 Docker、未登记 build、未动任何阶段


def test_container_deploy_state_defaults_match_pre_split_flags() -> None:
    """拆分前的局部标志初值：全部 False，host_port 在分配前为 0。"""
    fields = hosting._ContainerDeploy.__dataclass_fields__
    for name in ("fresh_port", "old_stopped", "stop_aborted", "up_completed"):
        assert fields[name].default is False
    assert fields["host_port"].default == 0


def test_host_container_stays_a_thin_orchestrator() -> None:
    """防回退：主函数只做编排，阶段细节在各自函数里（拆分前约 440 行）。"""
    lines = inspect.getsource(hosting.host_container).splitlines()
    assert len(lines) <= 110, f"host_container 又膨胀到 {len(lines)} 行"
    for name in (*_STAGES, *_POST_STAGES):
        assert callable(getattr(hosting, name))


# ---------------------------------------------------------------------------
# manager_api 历史类路由子模块
# ---------------------------------------------------------------------------

_HISTORY_PATHS = {
    ("/api/instances/{instance_id}/timeline", "GET"),
    ("/api/instances/{instance_id}/versions", "GET"),
    ("/api/instances/{instance_id}/rollback", "POST"),
}


def _routes() -> dict[tuple[str, str], Any]:
    from local_webpage_access.manager_api import _register_routes

    app = FastAPI()
    _register_routes(app)
    out: dict[tuple[str, str], Any] = {}
    for route in app.routes:
        for method in getattr(route, "methods", None) or ():
            out[(route.path, method)] = route
    return out


def test_history_routes_are_registered_from_submodule() -> None:
    routes = _routes()
    for key in _HISTORY_PATHS:
        assert key in routes, f"缺少路由 {key}"
        assert routes[key].endpoint.__module__ == "local_webpage_access.manager_history_routes"


def test_other_instance_routes_still_defined_in_manager_api() -> None:
    routes = _routes()
    for key in (
        ("/api/instances/{instance_id}", "GET"),
        ("/api/instances/{instance_id}/start", "POST"),
        ("/api/instances/{instance_id}/logs", "GET"),
    ):
        assert routes[key].endpoint.__module__ == "local_webpage_access.manager_api"


def test_history_routes_require_token_like_other_api_routes() -> None:
    from local_webpage_access.manager_api import require_token

    routes = _routes()
    for key in _HISTORY_PATHS:
        deps = [d.call for d in routes[key].dependant.dependencies]
        assert require_token in deps, f"{key} 未挂 token 鉴权"


def test_history_module_import_order_is_cycle_free() -> None:
    """子模块可被独立先行导入（它反向依赖 manager_api，manager_api 只惰性导入它）。"""
    import importlib
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import local_webpage_access.manager_history_routes as m; "
            "assert callable(m.register_history_routes)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert importlib.import_module("local_webpage_access.manager_api")
