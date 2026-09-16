"""issue #37 / #38：别名活验证空探针盲区与部署路径守卫覆盖。

#37：入口 HTML 只有 ``/assets/...`` 绝对路径时，探针列表为空却盖上
     aliasLiveVerified 印章。
#38：maybe_verify_alias_after_start 不跑守卫；探不到 HTML 时静默放行并盖章。
"""

from __future__ import annotations

import pytest

from local_webpage_access.errors import RecognitionError
from local_webpage_access.models import RouteMode, StaticConfig, Status
from local_webpage_access.path_alias import (
    _collect_alias_live_probe_paths,
    maybe_verify_alias_after_start,
    verify_alias_live,
)
from tests._helpers import make_static_manifest
from tests.test_issue21_alias_preservation import _CaddyFakeGW


_ABS_HTML = (
    '<script type="module" crossorigin src="/assets/index-C96cczuG.js"></script>\n'
    '<link rel="stylesheet" crossorigin href="/assets/index-BjfrsFYa.css">'
)


def _seed_aliased(workspace, registry, iid: str = "funasr-workbench"):
    workspace.app_dir(iid).mkdir(parents=True, exist_ok=True)
    manifest = make_static_manifest(
        iid,
        status=Status.RUNNING,
        static=StaticConfig(
            hostPort=21001,
            routeMode=RouteMode.NAME.value,
            routeHost=iid,
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return manifest


def test_collect_probes_empty_for_absolute_assets() -> None:
    assert _collect_alias_live_probe_paths(_ABS_HTML, "funasr-workbench") == []


def test_verify_alias_live_rejects_absolute_assets_empty_probes(config, monkeypatch) -> None:
    from local_webpage_access import path_alias

    def fake_probe(url, *, timeout=3.0):
        if url.rstrip("/").endswith("funasr-workbench"):
            return True, 200, "text/html", ("<!doctype html>" + _ABS_HTML).encode()
        return True, 200, "application/javascript", b"ok"

    monkeypatch.setattr(path_alias, "_http_probe_alias_resource", fake_probe)
    config.staticGatewayPort = 8080
    with pytest.raises(RecognitionError, match="绝对路径|白屏"):
        verify_alias_live(
            config, "funasr-workbench", entry_html=_ABS_HTML, instance_id="funasr-workbench"
        )


def test_verify_alias_live_plain_html_without_assets_still_passes(config, monkeypatch) -> None:
    from local_webpage_access import path_alias

    html = "<!doctype html><html><body><h1>hi</h1></body></html>"

    def fake_probe(url, *, timeout=3.0):
        return True, 200, "text/html", html.encode()

    monkeypatch.setattr(path_alias, "_http_probe_alias_resource", fake_probe)
    config.staticGatewayPort = 8080
    verify_alias_live(config, "plain", entry_html=html, instance_id="plain")


def test_maybe_verify_after_start_runs_guard_and_does_not_stamp(
    workspace, registry, config, monkeypatch
) -> None:
    from local_webpage_access import path_alias
    from local_webpage_access.models import InstanceManifest

    manifest = _seed_aliased(workspace, registry)
    iid = manifest.id
    monkeypatch.setattr(
        path_alias,
        "_fetch_entrypoint_html_for_alias_guard",
        lambda **_k: _ABS_HTML,
    )
    monkeypatch.setattr(path_alias, "StaticGateway", _CaddyFakeGW)
    monkeypatch.setattr(
        path_alias,
        "_http_probe_alias_resource",
        lambda *_a, **_k: (True, 200, "text/html", b"<!doctype html>"),
    )
    config.staticGatewayPort = 8080
    with pytest.raises(RecognitionError, match="绝对路径|白屏"):
        maybe_verify_alias_after_start(
            workspace, config, registry, iid, manifest, alias_fragment_preexisting=False
        )
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.aliasLiveVerifiedAt is None
    assert reloaded.aliasGuardResult == "failed"


def test_set_alias_records_skipped_guard_when_html_missing(
    workspace, registry, config, monkeypatch
) -> None:
    from local_webpage_access import path_alias
    from local_webpage_access.models import InstanceManifest
    from local_webpage_access.path_alias import set_instance_path_alias

    manifest = _seed_aliased(workspace, registry)
    iid = manifest.id
    # 清掉已有别名，模拟首次设置且探不到 HTML。
    manifest.static.routeMode = "port"
    manifest.static.routeHost = None
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    monkeypatch.setattr(
        path_alias, "_fetch_entrypoint_html_for_alias_guard", lambda **_k: None
    )
    monkeypatch.setattr(path_alias, "StaticGateway", _CaddyFakeGW)
    config.staticGatewayPort = 8080
    result = set_instance_path_alias(
        workspace, config, registry, iid, "funasr-workbench", skip_compat_check=True
    )
    assert result.live_verified is False
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.aliasGuardResult == "skipped"
    assert reloaded.aliasGuardCheckedAt
    assert reloaded.aliasLiveVerifiedAt is None
