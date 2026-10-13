"""HTML 资源引用分类（#49 / PLN-052 S1-03）。

旧实现把 ``//cdn.example/x.js`` 当成站内路径 ``/cdn.example/x.js``，
且外部 CDN 脚本先占用 bundle 样本名额，站内 bundle 反而抓不到。
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


@pytest.mark.parametrize(
    ("src", "kind", "path"),
    [
        ("https://cdn.example.com/lib.js", "external", None),
        ("//cdn.example.com/lib.js", "external", None),
        ("data:text/javascript,alert(1)", "inline", None),
        ("blob:https://x.example/abc", "inline", None),
        ("javascript:void(0)", "inline", None),
        ("", "inline", None),
        ("./assets/app.js", "same_origin", "/assets/app.js"),
        ("assets/app.js", "same_origin", "/assets/app.js"),
        ("../assets/app.js", "same_origin", "/assets/app.js"),
        ("/assets/app.js", "same_origin", "/assets/app.js"),
        ("/assets/app.js?v=3", "same_origin", "/assets/app.js"),
        ("/home-bookshelf/assets/app.js", "same_origin", "/home-bookshelf/assets/app.js"),
    ],
)
def test_classify_resource_ref_matrix(src: str, kind: str, path: str | None) -> None:
    assert classify_resource_ref(src, base_url="/") == (kind, path)


def test_relative_ref_resolves_against_alias_base() -> None:
    assert classify_resource_ref("assets/app.js", base_url="/demo/") == (
        "same_origin",
        "/demo/assets/app.js",
    )


def test_extract_js_bundle_paths_skips_external_before_counting() -> None:
    html = (
        '<script src="https://a.example/1.js"></script>'
        '<script src="//b.example/2.js"></script>'
        '<script src="https://c.example/3.js"></script>'
        '<script src="https://d.example/4.js"></script>'
        '<script src="assets/app.js"></script>'
    )
    assert _extract_js_bundle_paths(html, limit=4) == ["assets/app.js"]


def test_alias_probe_keeps_station_bundle_behind_cdn_scripts() -> None:
    html = (
        '<script src="https://a.example/1.js"></script>'
        '<script src="//b.example/2.js"></script>'
        '<script src="https://c.example/3.js"></script>'
        '<script src="https://d.example/4.js"></script>'
        '<script src="assets/app.js"></script>'
    )
    paths = _collect_alias_live_probe_paths(html, "quant-tools", limit=4)
    assert paths == ["/quant-tools/assets/app.js"]
    assert not any("cdn" in p or "example" in p for p in paths)


def test_alias_probe_never_treats_protocol_relative_as_path() -> None:
    html = '<script src="//cdn.example.com/lib.js"></script>'
    assert _collect_alias_live_probe_paths(html, "demo") == []


def test_resolve_alias_aware_external_returns_none_pair() -> None:
    assert _resolve_alias_aware_script_urls("//cdn.example.com/x.js", path_alias="demo") == (
        None,
        None,
    )


def test_collect_api_paths_never_fetches_external_bundles() -> None:
    html = (
        '<script src="https://a.example/1.js"></script>'
        '<script src="//cdn.example.com/lib.js"></script>'
        '<script src="assets/app.js"></script>'
    )
    fetched: list[str] = []

    def fake_fetch(url: str) -> str | None:
        fetched.append(url)
        return 'fetch("/api/v1/members")' if url.endswith("/assets/app.js") else None

    paths = _collect_api_paths(html, host_port=18000, fetch_text=fake_fetch)

    assert fetched == ["http://127.0.0.1:18000/assets/app.js"]
    assert "/api/v1/members" in paths
