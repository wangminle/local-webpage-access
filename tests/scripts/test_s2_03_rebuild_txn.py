"""S2-03 重建事务化：先建后换与失败补偿（PLN-052 S2-03 / D4 / D5 / D8）。

覆盖 host_container 在「已在跑的实例重建」时的编排契约：
- 成功：build（旧容器仍在服务）→ 救数据 → 停旧容器 → up；
- build 失败：旧容器从未被停，生成物恢复快照，实例状态不变；
- up / 验证失败（旧容器已停）：补偿回到旧版本并恢复服务；补偿失败则 FAILED 并列出残留；
- rebuildStrategy=stop-first：保留旧顺序（先停后建）；
- 无法保留旧镜像：零副作用中止。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from local_webpage_access import hosting, rebuild_txn
from local_webpage_access.errors import DockerError, HostingError
from local_webpage_access.models import InstanceManifest, Status
from tests.scripts._helpers import make_container_manifest

OLD_COMPOSE = "services:\n  app:\n    image: old\n"
NEW_COMPOSE = "services:\n  app:\n    image: new\n"


@pytest.fixture()
def deployed(workspace, registry):
    """已部署且在跑的容器实例；docker/ 下已有旧生成物。"""
    workspace.ensure_app_dirs("demo")
    manifest = make_container_manifest("demo", status=Status.RUNNING)
    manifest.appPath = str(workspace.app_current("demo"))
    manifest.container.containerId = "old-container"
    manifest.container.imageId = "old-image"
    manifest.container.hostPort = 21001
    manifest.container.composePath = str(workspace.app_compose_path("demo"))
    manifest.container.dockerfilePath = str(workspace.app_dockerfile_path("demo"))
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    workspace.app_docker("demo").mkdir(parents=True, exist_ok=True)
    workspace.app_compose_path("demo").write_text(OLD_COMPOSE, encoding="utf-8")
    return manifest


@pytest.fixture()
def env(workspace, registry, config, deployed, monkeypatch):
    """替换 Docker 与生成器，记录调用顺序；返回 (order, runtime, tags)。"""
    order: list[str] = []
    tags: list[tuple[str, str]] = []
    rt = MagicMock(name="runtime")
    rt.is_running.return_value = True
    rt.image_id.return_value = "old-image"
    rt.container_id.return_value = "new-container"

    def _rec(name, *, fail: bool = False, msg: str = ""):
        def _call(*a, **k):
            order.append(name)
            if fail:
                raise DockerError(msg or f"模拟 {name} 失败", instance_id="demo", action=name)

        return _call

    rt.build.side_effect = _rec("build")
    rt.down.side_effect = _rec("down")
    rt.up.side_effect = _rec("up")

    def _tag(src, dst):
        tags.append((src, dst))

    monkeypatch.setattr(hosting, "DockerRuntime", MagicMock(return_value=rt))
    monkeypatch.setattr(rebuild_txn, "tag_image", _tag)
    monkeypatch.setattr(
        "local_webpage_access.container_identity.ensure_non_root_identity_ready",
        lambda *a: None,
    )
    # S3-01：本机若恰好存在同名 <repo>:latest 镜像，留档会多打 b<id> 标签——
    # 本套件只验证 S2-03 事务语义，固定关闭留档解析保持确定性。
    monkeypatch.setattr(
        "local_webpage_access.versions.resolve_repo_image_id", lambda repo: None
    )
    monkeypatch.setattr(
        hosting,
        "_rescue_container_data_before_rebuild",
        lambda *a, **k: order.append("rescue"),
    )

    def _write_new_compose(*a, **k):
        workspace.app_compose_path("demo").write_text(NEW_COMPOSE, encoding="utf-8")

    monkeypatch.setattr(hosting, "generate_dockerfile", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_compose", _write_new_compose)
    monkeypatch.setattr(hosting, "generate_env", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "_ensure_container_port", lambda *a, **k: (21001, False))
    monkeypatch.setattr(hosting, "_wait_for_http", lambda *a, **k: True)
    monkeypatch.setattr(
        hosting,
        "_evaluate_container_verification",
        lambda *a, **k: {
            "overall_status": "passed",
            "liveness_passed": True,
            "mandatory_all_passed": True,
            "optional_warnings": [],
            "probe_notes": [],
            "observed_capabilities": ["ui"],
        },
    )
    return order, rt, tags


def _set_strategy(workspace, deployed, value: str) -> None:
    deployed.container.rebuildStrategy = value
    deployed.save(workspace.app_manifest_path("demo"))


def test_build_first_success_builds_before_stopping_old(
    workspace, registry, config, deployed, env
) -> None:
    order, rt, tags = env
    manifest = hosting.host_container(workspace, config, registry, "demo")

    assert order == ["build", "rescue", "down", "up"]
    assert len(tags) == 1 and tags[0][0] == "old-image" and tags[0][1].endswith(":prev")
    assert manifest.status == Status.RUNNING
    assert manifest.lastOperation and manifest.lastOperation["status"] == "succeeded"


def test_build_failure_keeps_old_container_and_restores_generated_files(
    workspace, registry, config, deployed, env
) -> None:
    order, rt, _ = env
    rt.build.side_effect = lambda *a, **k: (order.append("build"), _raise_docker())[0]

    with pytest.raises(DockerError, match="构建失败，旧版本仍在服务"):
        hosting.host_container(workspace, config, registry, "demo")

    assert "down" not in order and "up" not in order
    assert workspace.app_compose_path("demo").read_text(encoding="utf-8") == OLD_COMPOSE
    saved = hosting._load_manifest(workspace, "demo")
    assert saved.container.containerId == "old-container"
    assert saved.container.imageId == "old-image"
    assert saved.status == Status.RUNNING
    assert registry.get_instance("demo")["status"] == Status.RUNNING.value
    # 操作态的权威来源是 registry.last_operation_json（S2-02）；build 失败不改 manifest
    op = json.loads(registry.get_instance("demo")["last_operation_json"])
    assert op["status"] == "failed"
    assert op["kind"] == "rebuild"


def test_up_failure_after_stop_compensates_back_to_previous(
    workspace, registry, config, deployed, env
) -> None:
    order, rt, tags = env
    calls = {"up": 0}

    def _up(*a, **k):
        order.append("up")
        calls["up"] += 1
        if calls["up"] == 1:
            raise DockerError("新版本 up 失败", instance_id="demo", action="up")

    rt.up.side_effect = _up

    with pytest.raises(HostingError, match="已回滚到旧版本并恢复服务"):
        hosting.host_container(workspace, config, registry, "demo")

    assert order == ["build", "rescue", "down", "up", "down", "up"]
    # 补偿时 :prev 被重新打回 :latest，让 compose up 使用旧镜像
    assert tags[-1][1].endswith(":latest") and tags[-1][0].endswith(":prev")
    assert workspace.app_compose_path("demo").read_text(encoding="utf-8") == OLD_COMPOSE
    saved = hosting._load_manifest(workspace, "demo")
    assert saved.status == Status.RUNNING
    assert saved.lastOperation["status"] == "failed"
    assert saved.lastOperation["rollback"] == "restored"
    assert registry.get_instance("demo")["status"] == Status.RUNNING.value


def test_verification_failure_after_up_compensates(
    workspace, registry, config, deployed, env, monkeypatch
) -> None:
    order, rt, _ = env
    monkeypatch.setattr(
        hosting,
        "_evaluate_container_verification",
        lambda *a, **k: {
            "overall_status": "failed",
            "liveness_passed": True,
            "mandatory_all_passed": False,
            "error": "必选探针未通过",
        },
    )

    with pytest.raises(HostingError, match="已回滚到旧版本并恢复服务"):
        hosting.host_container(workspace, config, registry, "demo")

    assert order[-2:] == ["down", "up"]
    saved = hosting._load_manifest(workspace, "demo")
    assert saved.status == Status.RUNNING
    assert saved.lastOperation["rollback"] == "restored"


def test_compensation_failure_marks_failed_and_lists_residuals(
    workspace, registry, config, deployed, env, monkeypatch
) -> None:
    order, rt, _ = env
    rt.up.side_effect = lambda *a, **k: (order.append("up"), _raise_docker())[0]
    monkeypatch.setattr(hosting, "_wait_for_http", lambda *a, **k: False)

    with pytest.raises(HostingError, match="回滚未完成"):
        hosting.host_container(workspace, config, registry, "demo")

    saved = hosting._load_manifest(workspace, "demo")
    assert saved.status == Status.FAILED
    assert saved.lastOperation["rollback"] == "failed"
    assert saved.lastOperation["rollbackNotes"]


def test_stop_first_strategy_keeps_legacy_order(
    workspace, registry, config, deployed, env
) -> None:
    order, _, tags = env
    _set_strategy(workspace, deployed, "stop-first")

    hosting.host_container(workspace, config, registry, "demo")

    assert order.index("down") < order.index("build") < order.index("up")
    assert tags == []


def test_prepare_failure_aborts_before_touching_anything(
    workspace, registry, config, deployed, env, monkeypatch
) -> None:
    order, rt, _ = env

    def _tag_fail(src, dst):
        raise DockerError("docker tag 失败", instance_id="demo", action="tag")

    monkeypatch.setattr(rebuild_txn, "tag_image", _tag_fail)

    with pytest.raises(HostingError, match="无法保留旧镜像"):
        hosting.host_container(workspace, config, registry, "demo")

    assert order == []
    assert workspace.app_compose_path("demo").read_text(encoding="utf-8") == OLD_COMPOSE


def test_identity_save_failure_after_stop_compensates(
    workspace, registry, config, deployed, env, monkeypatch
) -> None:
    """S2 审计 P1-1：停机成功 → 清身份写盘失败 → 必须补偿回旧版本并恢复服务。

    旧代码 ``old_stopped`` 在 ``_clear_container_identity`` 之后才置位：身份写盘
    抛 OSError 时异常分支误判「旧版本仍在服务」，不补偿、registry 谎报 running，
    服务实际已下线（审计复现 docker 调用序止于 build → rescue → down）。
    """
    order, _rt, _tags = env
    original_save = InstanceManifest.save

    def _fail_when_identity_cleared(self, *args, **kwargs):
        # _clear_container_identity 落盘时机：containerId 已置 None
        if self.container is not None and self.container.containerId is None:
            raise OSError("模拟身份清空写盘失败")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(InstanceManifest, "save", _fail_when_identity_cleared)

    with pytest.raises(HostingError, match="已回滚到旧版本并恢复服务"):
        hosting.host_container(workspace, config, registry, "demo")

    # 旧容器确实停过（down）；补偿必须把旧版本重新拉起——最后一个 docker 动作是 up
    assert order[-1] == "up"
    assert order.count("down") >= 1
    assert workspace.app_compose_path("demo").read_text(encoding="utf-8") == OLD_COMPOSE
    saved = hosting._load_manifest(workspace, "demo")
    assert saved.status == Status.RUNNING
    assert saved.lastOperation["rollback"] == "restored"
    # registry 状态与实际服务一致（旧版本已被 up 恢复），不是无补偿的谎报
    assert registry.get_instance("demo")["status"] == Status.RUNNING.value


def test_rebuild_strategy_defaults_to_build_first(deployed) -> None:
    assert rebuild_txn.rebuild_strategy(deployed) == "build-first"
    deployed.container.rebuildStrategy = "stop-first"
    assert rebuild_txn.rebuild_strategy(deployed) == "stop-first"


def test_image_repo_follows_compose_default_naming(deployed) -> None:
    repo = rebuild_txn.image_repo(deployed)
    assert repo == f"{deployed.container.projectName}-{deployed.container.serviceName}"


def _raise_docker():
    raise DockerError("模拟失败", instance_id="demo", action="x")


def test_build_first_reuses_listening_registered_port(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-790：先建后换在 build 前分配端口时旧容器仍占用旧端口——按登记复用，不漂移。"""
    from local_webpage_access import hosting

    workspace.ensure_app_dirs("demo")
    manifest = make_container_manifest("demo", status=Status.RUNNING)
    manifest.container.hostPort = 21001
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)

    monkeypatch.setattr(hosting, "is_port_listening", lambda port: True)
    # DEV-166：分配器会真实 bind 探测端口池；并行/残留进程占着 21000 时旧断言会漂到 21001。
    # 这里模拟“只有旧容器的 21001 被占用、其余端口空闲”，让结果与宿主机状态无关。
    monkeypatch.setattr(
        "local_webpage_access.ports.is_port_in_use", lambda port, **kw: port == 21001
    )

    port, fresh = hosting._ensure_container_port(
        config, registry, "demo", allow_listening_reuse=True
    )
    assert (port, fresh) == (21001, False)

    # 不带旗标（首次部署/旧顺序）维持原语义：监听中不复用，重新分配
    port2, fresh2 = hosting._ensure_container_port(config, registry, "demo")
    assert fresh2 is True
    assert port2 != 21001
