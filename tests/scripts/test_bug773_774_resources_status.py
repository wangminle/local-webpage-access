"""BUG-773 / BUG-774 回归（2026-10-13 复审 F12 / F13）。

* BUG-773：畸形外部资源引用（无效 IPv6 host、异常 port 等）不得让
  ``classify_resource_ref`` 抛异常，也不得阻断同一 HTML 内其他有效站内资源的解析；
* BUG-774：已登记实例 manifest 缺失或路径为目录时，status 必须显式报告错误，
  ``source_kind`` 置未知（None），不得伪装成默认 zip 来源。
"""

from __future__ import annotations

import pytest

from local_webpage_access.access import (
    _collect_api_paths,
    _extract_js_bundle_paths,
    _resolve_alias_aware_script_urls,
    classify_resource_ref,
)
from local_webpage_access.path_alias import _collect_alias_live_probe_paths
from local_webpage_access.status import instance_status
from tests.scripts.test_health_status import _seed_static

_BAD_REFS = [
    "//[invalid/lib.js",
    "http://cdn.example.com:99999/lib.js",
    "https://[::1/lib.js",
]


@pytest.mark.parametrize("src", _BAD_REFS)
def test_classify_resource_ref_invalid_is_skipped_not_raised(src: str) -> None:
    assert classify_resource_ref(src, base_url="/") == ("invalid", None)


def test_bad_external_ref_does_not_block_station_bundle_parsing() -> None:
    html = (
        '<script src="//[invalid/lib.js"></script>'
        '<script src="http://cdn.example.com:99999/lib.js"></script>'
        '<script src="/assets/app.js"></script>'
    )
    assert _extract_js_bundle_paths(html) == ["/assets/app.js"]

    fetched: list[str] = []

    def fake_fetch(url: str) -> str | None:
        fetched.append(url)
        return 'fetch("/api/v1/members")' if url.endswith("/assets/app.js") else None

    paths = _collect_api_paths(html, host_port=18000, fetch_text=fake_fetch)

    assert fetched == ["http://127.0.0.1:18000/assets/app.js"]
    assert "/api/v1/members" in paths


def test_bad_external_ref_does_not_break_alias_resolution_and_live_probe() -> None:
    html = '<script src="//[invalid/lib.js"></script><script src="assets/app.js"></script>'
    assert _resolve_alias_aware_script_urls("//[invalid/lib.js", path_alias="demo") == (
        None,
        None,
    )
    assert _collect_alias_live_probe_paths(html, "demo") == ["/demo/assets/app.js"]


def test_missing_manifest_is_reported_with_unknown_source(workspace, registry, config) -> None:
    _seed_static(workspace, registry, "demo", host_port=21100)
    workspace.app_manifest_path("demo").unlink()

    st = instance_status(workspace, config, registry, "demo")

    assert st.source_kind is None
    assert st.manifest_error
    assert "FileNotFoundError" in st.manifest_error
    payload = st.to_dict()
    assert payload["sourceKind"] is None
    assert payload["manifestError"] == st.manifest_error


def test_manifest_path_is_directory_is_reported_with_unknown_source(
    workspace, registry, config
) -> None:
    _seed_static(workspace, registry, "demo", host_port=21100)
    manifest_path = workspace.app_manifest_path("demo")
    manifest_path.unlink()
    manifest_path.mkdir()

    st = instance_status(workspace, config, registry, "demo")

    assert st.source_kind is None
    assert st.manifest_error
    assert "IsADirectoryError" in st.manifest_error
    assert st.to_dict()["sourceKind"] is None
