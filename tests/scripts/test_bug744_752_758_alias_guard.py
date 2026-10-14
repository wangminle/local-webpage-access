"""S2-05 别名守卫收口（BUG-744 issue#52 / BUG-752 issue#58 / BUG-758 N5；D2/D3/D7）。

翻转自 design/5-checkpoints/2026-10-11/issues47-60-repro.py 的 test_52_* / test_58_* 与
2026-10-11/issues47-60-independent-repro.py 的 test_n5（原断言缺陷存在），
并新增：退出码 4、--ignore-alias-guard 审计事件、--update 更新前拒绝（D3）。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from local_webpage_access import path_alias
from local_webpage_access.errors import RecognitionError, ZipImportError
from local_webpage_access.importer import Importer
from local_webpage_access.models import (
    DesiredState,
    InstanceManifest,
    RouteMode,
    StaticConfig,
    Status,
)
from local_webpage_access.path_alias import (
    _classify_probe_failure,
    _collect_alias_live_probe_paths_with_coverage,
    alias_guard_exit_code,
    alias_guard_summary_line,
    maybe_verify_alias_after_start,
    verify_alias_live,
)
from tests.scripts._helpers import make_static_manifest
from tests.scripts.test_issue21_alias_preservation import _CaddyFakeGW


def _seed_aliased_static(workspace, registry, iid: str = "demo"):
    """已设别名（routeMode=name）的静态实例（存量：片段已在盘）。"""
    workspace.ensure_app_dirs(iid)
    current = workspace.app_current(iid)
    current.mkdir(parents=True, exist_ok=True)
    (current / "index.html").write_text("<!doctype html><title>old</title>", encoding="utf-8")
    manifest = make_static_manifest(
        iid,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        static=StaticConfig(
            hostPort=21001,
            routeMode=RouteMode.NAME.value,
            routeHost=iid,
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return manifest


def _write_zip(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    zp = tmp_path / name
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, content in files.items():
            zf.writestr(rel, content)
    return zp


# ---- #52（BUG-744）：故障分类与三态 ------------------------------------------


def test_52_existing_alias_content_failure_keeps_config_and_marks_failed_content(
    workspace, registry, config, monkeypatch
):
    """翻转 repro test_52_existing_alias_failure_returns_false：content 级失败
    记 failed(content)（退出码 4 的信号），别名配置保留。"""
    manifest = _seed_aliased_static(workspace, registry)
    iid = manifest.id
    monkeypatch.setattr(path_alias, "StaticGateway", _CaddyFakeGW)
    monkeypatch.setattr(
        path_alias, "_fetch_entrypoint_html_for_alias_guard", lambda **_k: None
    )

    def fail(*a, **k):
        raise RecognitionError("确定性的资源错误", kind="content")

    monkeypatch.setattr(path_alias, "verify_alias_live", fail)
    config.staticGatewayPort = 8080
    assert not maybe_verify_alias_after_start(
        workspace, config, registry, iid, manifest, alias_fragment_preexisting=True
    )
    assert manifest.aliasGuardResult == "failed(content)"
    assert manifest.aliasGuardError
    assert manifest.aliasGuardCheckedAt
    # issue #21 保留策略：别名与已验证标记不被清除。
    assert manifest.static is not None
    assert manifest.static.routeHost == iid


def test_52_existing_alias_transient_failure_only_marks_failed_transient(
    workspace, registry, config, monkeypatch
):
    """transient（连接拒绝 / 5xx / 超时）维持 issue #21 只告警：记
    failed(transient)（不触发退出码 4），别名保留。"""
    manifest = _seed_aliased_static(workspace, registry)
    iid = manifest.id
    monkeypatch.setattr(path_alias, "StaticGateway", _CaddyFakeGW)
    monkeypatch.setattr(
        path_alias, "_fetch_entrypoint_html_for_alias_guard", lambda **_k: None
    )

    def fail(*a, **k):
        raise RecognitionError("入口临时 5xx", kind="transient")

    monkeypatch.setattr(path_alias, "verify_alias_live", fail)
    config.staticGatewayPort = 8080
    assert not maybe_verify_alias_after_start(
        workspace, config, registry, iid, manifest, alias_fragment_preexisting=True
    )
    assert manifest.aliasGuardResult == "failed(transient)"
    assert alias_guard_exit_code(manifest) == 0
    assert manifest.static is not None
    assert manifest.static.routeHost == iid


def test_52_no_direct_html_still_checks_live_entry(config, monkeypatch):
    """活验证以别名入口实际响应为准：直达 HTML 缺失仍检查 live 入口的
    加载型绝对路径（content 级）。"""
    html = b'<html><link rel="stylesheet" href="/bad.css"></html>'
    monkeypatch.setattr(
        path_alias, "_http_probe_alias_resource", lambda *a, **k: (True, 200, "text/html", html)
    )
    config.staticGatewayPort = 8080
    with pytest.raises(RecognitionError, match="加载型绝对路径") as exc_info:
        verify_alias_live(config, "demo", entry_html=None, instance_id="demo")
    assert exc_info.value.kind == "content"


def test_probe_failure_classification() -> None:
    """连接错误 / 5xx / 超时 → transient；4xx 等 → content。"""
    assert _classify_probe_failure(None) == "transient"
    assert _classify_probe_failure(503) == "transient"
    assert _classify_probe_failure(404) == "content"


# ---- #58（BUG-752）：采样覆盖 -------------------------------------------------


def test_58_relative_stylesheet_modulepreload_img_collected() -> None:
    """翻转 repro test_58：相对 stylesheet / modulepreload / img 参与采样，
    拼别名前缀探测。"""
    html = (
        '<link rel="stylesheet" href="style.css">'
        '<link rel="modulepreload" href="m.js">'
        '<img src="img/a.png">'
    )
    paths, coverage = _collect_alias_live_probe_paths_with_coverage(html, "demo")
    assert paths == ["/demo/style.css", "/demo/m.js", "/demo/img/a.png"]
    assert coverage.checked == 3
    assert coverage.found == 3
    assert not coverage.truncated


def test_58_absolute_alias_prefixed_included_unprefixed_excluded() -> None:
    """带别名前缀的绝对资源参与探测；不带前缀的绝对加载型资源被守卫拦截，
    不参与采样（也不计入 found）。"""
    html = (
        '<link rel="stylesheet" href="/demo/style.css">'
        '<script src="/assets/bad.js"></script>'
    )
    paths, coverage = _collect_alias_live_probe_paths_with_coverage(html, "demo")
    assert paths == ["/demo/style.css"]
    assert coverage.found == 1


def test_58_limit_20_with_truncation_coverage() -> None:
    """采样上限由 6 提高到 20；超出时 truncated 标记并保留 found 计数。"""
    html = "".join(f'<script src="s{i}.js"></script>' for i in range(22))
    paths, coverage = _collect_alias_live_probe_paths_with_coverage(html, "demo")
    assert len(paths) == 20
    assert coverage.checked == 20
    assert coverage.found == 22
    assert coverage.truncated


def test_58_external_refs_skipped_and_counted() -> None:
    """外部 / 协议相对引用不占样本名额，计入 skippedExternal（#49 口径保留）。"""
    html = (
        '<script src="https://cdn.example/a.js"></script>'
        '<script src="//cdn.example/b.js"></script>'
        '<script src="local.js"></script>'
    )
    paths, coverage = _collect_alias_live_probe_paths_with_coverage(html, "demo")
    assert paths == ["/demo/local.js"]
    assert coverage.skippedExternal == 2
    assert coverage.found == 1


# ---- N5（BUG-758）：纯 API 契约（D7） -----------------------------------------


def test_n5_api_only_contract_passes_without_html(config, monkeypatch):
    """翻转 repro test_n5：servesUi=false（纯 API 服务）入口返回任意非 5xx
    即通过，跳过 HTML 与资源探针，记 passed(api-only)。"""
    monkeypatch.setattr(
        path_alias,
        "_http_probe_alias_resource",
        lambda *a, **k: (True, 200, "application/json", b'{"ok": true}'),
    )
    config.staticGatewayPort = 8080
    result = verify_alias_live(
        config, "demo", entry_html=None, instance_id="demo", serves_ui=False
    )
    assert result.api_only is True
    assert result.coverage.checked == 0


def test_n5_html_entry_still_required_when_serves_ui(config, monkeypatch):
    """servesUi 缺省为 True：入口未返回 HTML 仍是 content 级失败（行为不回退）。"""
    monkeypatch.setattr(
        path_alias,
        "_http_probe_alias_resource",
        lambda *a, **k: (True, 200, "application/json", b'{"ok": true}'),
    )
    config.staticGatewayPort = 8080
    with pytest.raises(RecognitionError, match="未返回 HTML") as exc_info:
        verify_alias_live(config, "demo", entry_html=None, instance_id="demo")
    assert exc_info.value.kind == "content"


def test_maybe_verify_marks_passed_api_only(workspace, registry, config, monkeypatch):
    """纯 API 实例启动后守卫记 passed(api-only) 并写采样覆盖。"""
    manifest = _seed_aliased_static(workspace, registry)
    iid = manifest.id
    manifest.capabilityContract = {"servesUi": False, "servesApi": True}
    manifest.save(workspace.app_manifest_path(iid))
    monkeypatch.setattr(path_alias, "StaticGateway", _CaddyFakeGW)
    monkeypatch.setattr(
        path_alias, "_fetch_entrypoint_html_for_alias_guard", lambda **_k: None
    )
    monkeypatch.setattr(
        path_alias,
        "_http_probe_alias_resource",
        lambda *a, **k: (True, 200, "application/json", b'{"ok": true}'),
    )
    config.staticGatewayPort = 8080
    assert maybe_verify_alias_after_start(
        workspace, config, registry, iid, manifest, alias_fragment_preexisting=True
    )
    assert manifest.aliasGuardResult == "passed(api-only)"
    assert manifest.aliasLiveVerifiedAt
    assert manifest.aliasProbeCoverage == {
        "checked": 0,
        "found": 0,
        "truncated": False,
        "skippedExternal": 0,
    }


# ---- 收口摘要与退出码（D2） ---------------------------------------------------


def test_alias_guard_exit_code_4_only_for_content_failure() -> None:
    manifest = make_static_manifest(
        "demo",
        static=StaticConfig(routeMode=RouteMode.NAME.value, routeHost="demo"),
    )
    for result, expected in [
        ("passed", 0),
        ("passed(api-only)", 0),
        ("failed(content)", 4),
        ("failed(transient)", 0),
        ("skipped(非 Caddy 后端)", 0),
        ("skipped(显式跳过)", 0),
        ("skipped(实例未运行)", 0),
        (None, 0),
    ]:
        manifest.aliasGuardResult = result
        assert alias_guard_exit_code(manifest) == expected, result


def test_alias_guard_summary_line_format() -> None:
    manifest = make_static_manifest(
        "demo",
        static=StaticConfig(routeMode=RouteMode.NAME.value, routeHost="demo"),
    )
    manifest.aliasGuardResult = "failed(content)"
    assert alias_guard_summary_line(manifest) == "别名 /demo/:failed(content)"
    # 无别名：无摘要行。
    manifest.static = StaticConfig(routeMode=RouteMode.PORT.value)
    assert alias_guard_summary_line(manifest) is None


def test_maybe_verify_skip_compat_check_stamps_explicit_skip_and_audits(
    workspace, registry, config, monkeypatch
):
    """--ignore-alias-guard（skip_compat_check）记 skipped(显式跳过) 并写审计事件。"""
    manifest = _seed_aliased_static(workspace, registry)
    iid = manifest.id
    monkeypatch.setattr(path_alias, "StaticGateway", _CaddyFakeGW)
    config.staticGatewayPort = 8080
    assert not maybe_verify_alias_after_start(
        workspace,
        config,
        registry,
        iid,
        manifest,
        skip_compat_check=True,
        alias_fragment_preexisting=True,
    )
    assert manifest.aliasGuardResult == "skipped(显式跳过)"
    events = registry.list_events(iid)
    assert any("ignore-alias-guard" in (e.get("message") or "") for e in events)


# ---- D3（BUG-744）：--update 更新前守卫 ---------------------------------------


def test_update_refuses_before_swap_when_existing_alias_and_new_content_fails_guard(
    workspace, registry, config, tmp_path
):
    """D3：实例已有别名、--update 未显式传 --path-alias 时，新内容在换入前
    过同一道守卫；未通过即拒绝且 current/ 零副作用，拒绝文案给出补救命令。"""
    manifest = _seed_aliased_static(workspace, registry)
    iid = manifest.id
    old_html = (workspace.app_current(iid) / "index.html").read_text(encoding="utf-8")
    new_zip = _write_zip(
        tmp_path,
        "new.zip",
        {
            "index.html": (
                '<!doctype html><script type="module" src="/assets/index.js"></script>'
            ),
            "assets/index.js": "console.log(1)",
        },
    )
    importer = Importer(workspace, config, registry)
    with pytest.raises(ZipImportError, match="别名守卫") as exc_info:
        importer.update_zip(new_zip, iid, restart=False)
    assert "--ignore-alias-guard" in str(exc_info.value)
    assert "--base=/demo/" in str(exc_info.value)
    # 换入前拒绝：current/ 未被修改，别名保留。
    assert (workspace.app_current(iid) / "index.html").read_text(encoding="utf-8") == old_html
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.static is not None
    assert reloaded.static.routeHost == iid


def test_update_alias_guard_passes_clean_content(workspace, registry, config, tmp_path):
    """新内容不含未前缀绝对资源时，D3 守卫放行，更新成功且别名保留。"""
    manifest = _seed_aliased_static(workspace, registry)
    iid = manifest.id
    new_zip = _write_zip(
        tmp_path,
        "new.zip",
        {
            "index.html": (
                '<!doctype html><script type="module" src="/demo/assets/index.js"></script>'
            ),
            "assets/index.js": "console.log(1)",
        },
    )
    importer = Importer(workspace, config, registry)
    result = importer.update_zip(new_zip, iid, restart=False)
    assert not result.skipped
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.static is not None
    assert reloaded.static.routeHost == iid


def test_update_ignore_alias_guard_overrides_and_audits(
    workspace, registry, config, tmp_path
):
    """--ignore-alias-guard 越过更新前守卫（记审计事件），更新照常完成。"""
    manifest = _seed_aliased_static(workspace, registry)
    iid = manifest.id
    new_zip = _write_zip(
        tmp_path,
        "new.zip",
        {
            "index.html": (
                '<!doctype html><script type="module" src="/assets/index.js"></script>'
            ),
            "assets/index.js": "console.log(1)",
        },
    )
    importer = Importer(workspace, config, registry)
    result = importer.update_zip(new_zip, iid, restart=False, ignore_alias_guard=True)
    assert not result.skipped
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.static is not None
    assert reloaded.static.routeHost == iid
    events = registry.list_events(iid)
    assert any(
        "--ignore-alias-guard" in (e.get("message") or "") for e in events
    )


# ---- 管理页 API 透出 -----------------------------------------------------------


def test_status_exposes_alias_guard_fields(workspace, registry, config, monkeypatch):
    """status 快照（管理页 /api/instances 与 lwa status 共用）透出守卫三态、
    最近失败原因、活验证时间与采样覆盖。"""
    from local_webpage_access.status import instance_status

    manifest = _seed_aliased_static(workspace, registry)
    iid = manifest.id
    manifest.aliasGuardResult = "failed(content)"
    manifest.aliasGuardCheckedAt = "2026-10-13T10:00:00+08:00"
    manifest.aliasGuardError = "别名资源 /demo/a.js 返回了 HTML"
    manifest.aliasLiveVerifiedAt = "2026-10-01T00:00:00+08:00"
    manifest.aliasProbeCoverage = {
        "checked": 3,
        "found": 5,
        "truncated": True,
        "skippedExternal": 2,
    }
    manifest.save(workspace.app_manifest_path(iid))
    monkeypatch.setattr(
        "local_webpage_access.status._resolve_network_urls",
        lambda *a: dict(
            lan_url=None,
            route_host=iid,
            route_url=None,
            current_lan_ip=None,
            persisted_lan_ip=None,
            lan_address_stale=False,
            lan_url_source="test",
        ),
    )
    payload = instance_status(workspace, config, registry, iid).to_dict()
    assert payload["aliasGuardResult"] == "failed(content)"
    assert payload["aliasGuardCheckedAt"] == "2026-10-13T10:00:00+08:00"
    assert payload["aliasGuardError"]
    assert payload["aliasLiveVerifiedAt"]
    assert payload["aliasProbeCoverage"]["truncated"] is True
