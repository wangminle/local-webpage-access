"""S2-09 管理页乐观并发回归（BUG-751 / issue #57-3）。

status 与 manager 实例 API 输出 ``revision``；生命周期接口与更新流程接受可选
``expectedRevision``，冲突返回 ``409 revision_conflict``；前端操作携带列表快照
的 revision，遇 409 提示刷新。CLI 是单用户交互，不参与（WBS 明确不加参数）。
"""

from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_webpage_access.config import Config, PortPool
from local_webpage_access.manager_api import create_app, ensure_token
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "src" / "local_webpage_access" / "manager_static" / "app.js"


# ---- 夹具 -------------------------------------------------------------------


def _write_config(ws: Workspace) -> None:
    from local_webpage_access.config import example_config_text

    if not ws.config_path.is_file():
        ws.config_path.write_text(example_config_text(), encoding="utf-8")


def _make_static_zip(path: Path, html: str = "<h1>hello</h1>") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("index.html", html)


@pytest.fixture()
def manager_env(workspace_root: Path):
    """带一个静态实例的管理页环境（同 test_manager_api.manager_env）。"""
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    _write_config(ws)
    config = Config(
        staticGateway="builtin",
        portPool=PortPool(start=21000, end=21050),
    )
    reg = Registry(ws.db_path)
    reg.open()

    from local_webpage_access.importer import Importer

    zip_path = ws.inbox / "static.zip"
    _make_static_zip(zip_path)
    importer = Importer(ws, config, reg)
    result = importer.import_zip(str(zip_path))

    token = ensure_token(ws)
    app = create_app(ws, config, reg, token=token)
    with TestClient(app) as client:
        yield {
            "workspace": ws,
            "config": config,
            "registry": reg,
            "client": client,
            "token": token,
            "instance_id": result.instance_id,
        }


def _headers(env: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {env['token']}"}


# ---- revision 输出 ------------------------------------------------------------


def test_instances_api_outputs_revision(manager_env: dict) -> None:
    """S2-09：列表与详情 API 输出 registry revision（新实例默认为 1）。"""
    iid = manager_env["instance_id"]
    resp = manager_env["client"].get("/api/instances", headers=_headers(manager_env))
    assert resp.status_code == 200
    row = next(x for x in resp.json()["instances"] if x["id"] == iid)
    assert row["revision"] == 1

    resp = manager_env["client"].get(f"/api/instances/{iid}", headers=_headers(manager_env))
    assert resp.status_code == 200
    assert resp.json()["instance"]["revision"] == 1


def test_instance_status_snapshot_outputs_revision(manager_env: dict) -> None:
    """S2-09：status.instance_status 快照（CLI lwa status 同口径）输出 revision。"""
    from local_webpage_access.status import instance_status

    snap = instance_status(
        manager_env["workspace"],
        manager_env["config"],
        manager_env["registry"],
        manager_env["instance_id"],
    )
    assert snap.revision == 1
    assert snap.to_dict()["revision"] == 1
    # registry 递增后快照跟随
    manager_env["registry"].cas_increment_revision(manager_env["instance_id"], 1)
    snap = instance_status(
        manager_env["workspace"],
        manager_env["config"],
        manager_env["registry"],
        manager_env["instance_id"],
    )
    assert snap.to_dict()["revision"] == 2


# ---- 生命周期接口：expectedRevision -------------------------------------------


def test_stop_with_expected_revision_ok(manager_env: dict) -> None:
    """携带匹配 revision 的 stop 正常受理，且透传 expected_revision 到 lifecycle。"""
    iid = manager_env["instance_id"]
    seen: list[int | None] = []

    def fake_stop(ws, cfg, reg, iid, **kwargs):
        seen.append(kwargs.get("expected_revision"))
        from local_webpage_access.models import InstanceManifest

        return InstanceManifest.load(ws.app_manifest_path(iid))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("local_webpage_access.lifecycle.stop_instance_op", fake_stop)
        resp = manager_env["client"].post(
            f"/api/instances/{iid}/stop?expectedRevision=1",
            headers=_headers(manager_env),
        )
    assert resp.status_code == 200, resp.text
    assert seen == [1]


def test_stop_with_stale_expected_revision_409(manager_env: dict) -> None:
    """revision 已被其它通道递增后，携带旧 revision 的操作返回 409 revision_conflict。"""
    iid = manager_env["instance_id"]
    manager_env["registry"].cas_increment_revision(iid, 1)  # 模拟并发更新：1 -> 2

    resp = manager_env["client"].post(
        f"/api/instances/{iid}/stop?expectedRevision=1",
        headers=_headers(manager_env),
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "revision_conflict"
    assert body["error"]["actual"] == 2


def test_stop_without_expected_revision_compatible(manager_env: dict) -> None:
    """不传 expectedRevision 保持兼容：原有调用（含旧前端/脚本）不受影响。"""
    iid = manager_env["instance_id"]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "local_webpage_access.lifecycle.stop_instance_op",
            lambda ws, cfg, reg, iid: None,
        )
        resp = manager_env["client"].post(
            f"/api/instances/{iid}/stop",
            headers=_headers(manager_env),
        )
    assert resp.status_code == 200, resp.text


def test_start_accepts_expected_revision_query(manager_env: dict) -> None:
    """start 路由接受 expectedRevision 并透传（fallback_policy 共存）。"""
    iid = manager_env["instance_id"]
    seen: list[int | None] = []

    def fake_start(ws, cfg, reg, iid, **kwargs):
        seen.append(kwargs.get("expected_revision"))
        from local_webpage_access.models import InstanceManifest

        return InstanceManifest.load(ws.app_manifest_path(iid))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("local_webpage_access.lifecycle.start_instance", fake_start)
        resp = manager_env["client"].post(
            f"/api/instances/{iid}/start?fallback_policy=disabled&expectedRevision=1",
            headers=_headers(manager_env),
        )
    assert resp.status_code == 200, resp.text
    assert seen == [1]


def test_restart_rebuild_recover_routes_accept_expected_revision(manager_env: dict) -> None:
    """restart/rebuild/recover 三个路由同样接受 expectedRevision。"""
    iid = manager_env["instance_id"]
    from local_webpage_access.models import InstanceManifest

    def fake_manifest(ws, cfg, reg, iid, **kwargs):
        return InstanceManifest.load(ws.app_manifest_path(iid))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("local_webpage_access.lifecycle.restart_instance", fake_manifest)
        mp.setattr("local_webpage_access.lifecycle.rebuild_instance", fake_manifest)
        mp.setattr("local_webpage_access.lifecycle.recover_instance", fake_manifest)
        for op in ("restart", "rebuild", "recover"):
            resp = manager_env["client"].post(
                f"/api/instances/{iid}/{op}?expectedRevision=1",
                headers=_headers(manager_env),
            )
            assert resp.status_code == 200, f"{op}: {resp.text}"


def test_recover_stale_expected_revision_409(manager_env: dict) -> None:
    """recover 不接受 expected_revision 形参（lifecycle 属 S2-03 并行改动范围），
    但 _lifecycle_op 的锁外预检仍对其生效。"""
    iid = manager_env["instance_id"]
    manager_env["registry"].cas_increment_revision(iid, 1)
    resp = manager_env["client"].post(
        f"/api/instances/{iid}/recover?expectedRevision=1",
        headers=_headers(manager_env),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "revision_conflict"


def test_lifecycle_revision_conflict_maps_409(manager_env: dict) -> None:
    """S2-09：锁内 LifecycleError(code=revision_conflict) 经全局处理器映射 409，
    不再被 LifecycleError→internal 吞成 500。"""
    from local_webpage_access.errors import LifecycleError

    iid = manager_env["instance_id"]

    def conflicting_rebuild(ws, cfg, reg, iid, **kwargs):
        raise LifecycleError(
            "实例 revision 与期望不符",
            code="revision_conflict",
            instance_id=iid,
            expected=1,
            actual=2,
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("local_webpage_access.lifecycle.rebuild_instance", conflicting_rebuild)
        resp = manager_env["client"].post(
            f"/api/instances/{iid}/rebuild?expectedRevision=1",
            headers=_headers(manager_env),
        )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "revision_conflict"
    assert body["error"]["detail"]["actual"] == 2


# ---- 更新流程 ------------------------------------------------------------------


def test_update_endpoint_revision_conflict_and_success(manager_env: dict) -> None:
    """update（zip）携带 expectedRevision：旧值 409，匹配值 200 且 revision 递增。"""
    iid = manager_env["instance_id"]
    ws: Workspace = manager_env["workspace"]
    v2 = ws.inbox / "static-v2.zip"
    _make_static_zip(v2, html="<h1>v2</h1>")

    # 并发通道先更新（revision 1 -> 2），前端快照 revision=1 过期
    manager_env["registry"].cas_increment_revision(iid, 1)
    resp = manager_env["client"].post(
        f"/api/instances/{iid}/update",
        headers=_headers(manager_env),
        json={"zipPath": "static-v2.zip", "restart": False, "expectedRevision": 1},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "revision_conflict"

    # 用当前 revision 重试：成功且响应里 revision 已递增（import CAS 1 -> 2 是并发
    # 写入产生的；本次 update 收尾 CAS 在此基础上再 +1）
    resp = manager_env["client"].post(
        f"/api/instances/{iid}/update",
        headers=_headers(manager_env),
        json={"zipPath": "static-v2.zip", "restart": False, "expectedRevision": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] is False
    assert body["instance"]["revision"] > 2


def test_update_endpoint_without_expected_revision_compatible(manager_env: dict) -> None:
    """不传 expectedRevision 的 update 保持兼容（既有行为不变）。"""
    iid = manager_env["instance_id"]
    ws: Workspace = manager_env["workspace"]
    v2 = ws.inbox / "static-v2.zip"
    _make_static_zip(v2, html="<h1>v2</h1>")
    resp = manager_env["client"].post(
        f"/api/instances/{iid}/update",
        headers=_headers(manager_env),
        json={"zipPath": "static-v2.zip", "restart": False},
    )
    assert resp.status_code == 200, resp.text


def test_update_endpoint_bad_expected_revision_type_400(manager_env: dict) -> None:
    """expectedRevision 类型非法（字符串/布尔）直接 400，不静默忽略。"""
    iid = manager_env["instance_id"]
    ws: Workspace = manager_env["workspace"]
    v2 = ws.inbox / "static-v2.zip"
    _make_static_zip(v2, html="<h1>v2</h1>")
    for bad in ("1", True):
        resp = manager_env["client"].post(
            f"/api/instances/{iid}/update",
            headers=_headers(manager_env),
            json={"zipPath": "static-v2.zip", "restart": False, "expectedRevision": bad},
        )
        assert resp.status_code == 400, bad
        assert resp.json()["error"]["code"] == "bad_request"


def test_update_from_dir_skipped_path_honors_expected_revision(
    manager_env: dict, tmp_path: Path
) -> None:
    """S2-09：update_from_dir 的指纹未变 skipped 路径同样受 expected_revision
    约束（CHK-353 语义对齐），冲突抛 ZipImportError(revision_conflict)。"""
    from local_webpage_access.importer import Importer

    env = manager_env
    iid = env["instance_id"]
    ws: Workspace = env["workspace"]
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.html").write_text("<h1>hello</h1>", encoding="utf-8")

    importer = Importer(ws, env["config"], env["registry"])
    # 先切为 folder 源（不带 revision 约束）
    result = importer.update_from_dir(iid, source_dir=src, restart=False)
    assert result.skipped is False

    env["registry"].cas_increment_revision(iid, env["registry"].get_revision(iid))
    stale = env["registry"].get_revision(iid) - 1
    from local_webpage_access.errors import ZipImportError

    with pytest.raises(ZipImportError) as excinfo:
        importer.update_from_dir(iid, restart=False, expected_revision=stale)
    assert excinfo.value.code == "revision_conflict"

    # 匹配当前 revision 时 skipped 路径正常返回
    result = importer.update_from_dir(
        iid, restart=False, expected_revision=env["registry"].get_revision(iid)
    )
    assert result.skipped is True


def test_update_from_git_accepts_expected_revision(
    manager_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2-09：update_from_git 接受 expected_revision 并做锁外预检（skipped 路径）。"""
    from local_webpage_access import git_source
    from local_webpage_access.importer import Importer

    env = manager_env
    iid = env["instance_id"]
    reg: Registry = env["registry"]

    # 造一个 git 源实例：直接改写 manifest 身份字段
    from local_webpage_access.models import InstanceManifest

    mpath = env["workspace"].app_manifest_path(iid)
    manifest = InstanceManifest.load(mpath)
    manifest.sourceKind = "git"
    manifest.sourceGitUrl = "https://github.com/example/repo"
    manifest.sourceGitRef = "main"
    manifest.sourceGitRefKind = "branch"
    manifest.sourceGitCommit = "a" * 40
    manifest.save(mpath)

    # OID 未变 → skipped（零外网）
    monkeypatch.setattr(git_source, "probe_remote_commit", lambda *a, **k: "a" * 40)
    importer = Importer(env["workspace"], env["config"], reg)
    result = importer.update_from_git(iid, restart=False)
    assert result.skipped is True

    # 过期 revision → 冲突（即便会 skipped）
    current = reg.get_revision(iid)
    reg.cas_increment_revision(iid, current)
    from local_webpage_access.errors import ZipImportError

    with pytest.raises(ZipImportError) as excinfo:
        importer.update_from_git(iid, restart=False, expected_revision=current)
    assert excinfo.value.code == "revision_conflict"


# ---- 前端 ---------------------------------------------------------------------


def _run_node(script: str) -> None:
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    run_env = {**os.environ, **env}
    prelude = 'const fs = require("node:fs");\nconst vm = require("node:vm");\n'
    subprocess.run(["node", "-e", prelude + script], check=True, env=run_env)


def _hooks_prelude() -> str:
    """在 Node vm 中加载 app.js 并用桩构造 app（钩子随之注册到 __LWA_TEST_HOOKS__）。"""
    app_js = APP_JS.read_text(encoding="utf-8")
    return f"""
const assert = require("node:assert");
const context = {{ window: {{ __LWA_TEST_HOOKS__: {{}}, LWA: {{}} }}, console: console,
  URLSearchParams: URLSearchParams }};
vm.runInNewContext({app_js!r}, context);
context.window.LWA.createManagerApp(
  {{ createApp: function (r) {{ return {{ mount: function () {{}} }}; }} }},
  {{
    document: null,
    fetch: function () {{ return Promise.resolve({{ ok: true, json: function () {{ return Promise.resolve({{}}); }} }}); }},
    location: {{ hostname: "127.0.0.1", search: "" }},
    sessionStorage: {{ getItem: function () {{ return null; }}, setItem: function () {{}}, removeItem: function () {{}} }},
    history: {{ replaceState: function () {{}} }},
    setInterval: function () {{ return 0; }},
    setTimeout: function (fn) {{ fn(); }},
    clearTimeout: function () {{}},
    clearInterval: function () {{}},
    URLSearchParams: URLSearchParams,
  }}
);
const h = context.window.__LWA_TEST_HOOKS__;
"""


def test_frontend_carries_revision_in_op_url() -> None:
    """S2-09：opUrlWithRevision 携带列表快照 revision；缺失时保持原 URL。"""
    _run_node(
        _hooks_prelude()
        + """
assert.strictEqual(
  h.opUrlWithRevision("demo", "stop", 3),
  "/api/instances/demo/stop?expectedRevision=3"
);
assert.strictEqual(
  h.opUrlWithRevision("demo", "rebuild", 1),
  "/api/instances/demo/rebuild?expectedRevision=1"
);
// revision 缺失/非数字：不带参数（兼容旧后端）
assert.strictEqual(h.opUrlWithRevision("demo", "stop", undefined), "/api/instances/demo/stop");
assert.strictEqual(h.opUrlWithRevision("demo", "stop", null), "/api/instances/demo/stop");
assert.strictEqual(h.opUrlWithRevision("demo", "stop", "3"), "/api/instances/demo/stop");
// id 需要 URL 编码
assert.ok(h.opUrlWithRevision("a/b", "stop", 2).indexOf("a%2Fb") !== -1);
"""
    )


def test_frontend_body_with_revision() -> None:
    """S2-09：bodyWithRevision 注入 expectedRevision 且保留原字段；无 revision 原样。"""
    _run_node(
        _hooks_prelude()
        + """
const body = h.bodyWithRevision({ restart: true, keepData: true }, 7);
// 跨 vm realm 的对象 deepStrictEqual 原型不同，改比 JSON
assert.strictEqual(JSON.stringify(body), JSON.stringify({ restart: true, keepData: true, expectedRevision: 7 }));
assert.strictEqual(JSON.stringify(h.bodyWithRevision({ restart: true }, undefined)), JSON.stringify({ restart: true }));
assert.strictEqual(JSON.stringify(h.bodyWithRevision(null, 7)), JSON.stringify({ expectedRevision: 7 }));
"""
    )


def test_frontend_revision_conflict_detection_and_prompt() -> None:
    """S2-09：isRevisionConflict 识别 error.code；提示文案为「请刷新后重试」。"""
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "实例已被其他操作更新，请刷新后重试" in app_js
    _run_node(
        _hooks_prelude()
        + """
assert.strictEqual(h.isRevisionConflict({ code: "revision_conflict" }), true);
assert.strictEqual(h.isRevisionConflict({ code: "cancelling" }), false);
assert.strictEqual(h.isRevisionConflict(null), false);
assert.strictEqual(h.isRevisionConflict(new Error("x")), false);
"""
    )


def test_frontend_lifecycle_ops_attach_revision() -> None:
    """S2-09：doOperation 为生命周期操作拼 expectedRevision；cancel-build 不携带。"""
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "opUrlWithRevision(id, op, revInst && revInst.revision)" in app_js
    assert 'if (op !== "cancel-build")' in app_js
    # 三个更新/降级入口也携带
    assert app_js.count("bodyWithRevision(") >= 2
    assert 'opUrlWithRevision(id, "confirm-fallback", fbInst && fbInst.revision)' in app_js
