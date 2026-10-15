"""BUG-843：access review 的 API 探测口径（GET 证据语义 + 拼接折叠 + 不确定分级）。

证据来自 CHK-401 E 项现网实测三例：POST-only 端点被 GET 404 误判错位
（prd-review-v035）、bundle 前缀拼接形态抽不出真实路径（funasr-workbench）、
const 拼接字面量被当绝对路径（patent-disclosure-workflow-v1）。
"""

from __future__ import annotations


from local_webpage_access import access
from local_webpage_access.access import (
    InstanceAccessReport,
    UrlProbe,
    _check_api_paths,
    _extract_api_paths,
)
from local_webpage_access.config import Config


def _probe(status=None, length=None, ctype=None):
    p = UrlProbe(url="http://127.0.0.1/x")
    p.status_code = status
    p.content_length = length
    p.content_type = ctype
    p.ok = status is not None and 200 <= status < 300
    return p


def _run_check(monkeypatch, html, *, absolute, prefixed, alias="demo"):
    """以受控探测结果执行 _check_api_paths，返回 (rep, calls)。"""
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return prefixed if f"/{alias}/" in url else absolute

    monkeypatch.setattr(access, "_http_get", fake_get)
    rep = InstanceAccessReport(instance_id="demo", path_alias=alias)
    _check_api_paths(
        rep,
        Config(staticGateway="caddy", staticGatewayPort=8080),
        alias,
        html,
    )
    return rep


def test_concat_context_literal_not_extracted() -> None:
    """`BASE + '/api/...'` 拼接形态的字面量不是绝对路径证据（patent 形态）。"""
    js = 'const API="/demo";fetch(API+"/api/status");'
    assert _extract_api_paths(js) == []
    # 压缩产物形如 af="/demo/"+e 同理；独立字面量仍应抽取
    assert _extract_api_paths('fetch("/api/v1/members")') == ["/api/v1/members"]
    assert _extract_api_paths('af="/demo/"+"/api/x"') == []


def test_post_only_endpoints_are_uncertain_not_mismatch(monkeypatch) -> None:
    """两侧 404（prd-review 形态：POST-only 端点）→ 不确定，不计错位不升 WARN。"""
    rep = _run_check(
        monkeypatch,
        'axios.post("/api/auth/login")',
        absolute=_probe(status=404, length=22, ctype="application/json"),
        prefixed=_probe(status=404, length=22, ctype="application/json"),
    )
    assert rep.api_findings and not any(f.mismatch for f in rep.api_findings)
    assert all(f.uncertain for f in rep.api_findings)
    assert rep.status != "warn"
    assert any("不确定" in f for f in rep.findings)


def test_old_probable_branch_removed(monkeypatch) -> None:
    """根 404 + 带前缀非空 JSON 404（旧「疑似」分支）不再判错位。"""
    rep = _run_check(
        monkeypatch,
        'fetch("/api/v1/x")',
        absolute=_probe(status=404, length=0),
        prefixed=_probe(status=404, length=64, ctype="application/json"),
    )
    assert not any(f.mismatch for f in rep.api_findings)
    assert rep.findings and all("路径错位：" not in f for f in rep.findings)


def test_real_mismatch_still_detected(monkeypatch) -> None:
    """真错位（根 404、带前缀 200 有实体）仍要检出并升 WARN。"""
    rep = _run_check(
        monkeypatch,
        'fetch("/api/v1/members")',
        absolute=_probe(status=404, length=0),
        prefixed=_probe(status=200, length=128, ctype="application/json"),
    )
    assert rep.api_findings[0].mismatch is True
    assert rep.api_findings[0].uncertain is False
    assert rep.status == "warn"
    assert any("路径错位" in f for f in rep.findings)


def test_mismatch_with_prefixed_auth_route(monkeypatch) -> None:
    """带前缀 401（路由存在、需鉴权）+ 根 404 也构成错位证据。"""
    rep = _run_check(
        monkeypatch,
        'fetch("/api/v1/me")',
        absolute=_probe(status=404, length=0),
        prefixed=_probe(status=401, length=31, ctype="application/json"),
    )
    assert rep.api_findings[0].mismatch is True


def test_absolute_route_exists_is_not_mismatch(monkeypatch) -> None:
    """根 405/401（路由存在、方法/鉴权受限）不判错位也不算不确定。"""
    for status in (405, 401, 422):
        rep = _run_check(
            monkeypatch,
            'fetch("/api/auth/login")',
            absolute=_probe(status=status, length=12, ctype="application/json"),
            prefixed=_probe(status=status, length=12, ctype="application/json"),
        )
        assert not any(f.mismatch or f.uncertain for f in rep.api_findings), status


def test_default_paths_uncertain_not_reported(monkeypatch) -> None:
    """默认兜底路径的 404/404 保持静默（无 API 项目不产噪音）。"""
    rep = _run_check(
        monkeypatch,
        html=None,
        absolute=_probe(status=404, length=0),
        prefixed=_probe(status=404, length=0),
    )
    assert rep.api_findings and not any(f.mismatch for f in rep.api_findings)
    assert rep.findings == []


def test_empty_200_root_still_counts_missing(monkeypatch) -> None:
    """根空 200 的经典形态仍判错位。"""
    rep = _run_check(
        monkeypatch,
        'fetch("/api/")',
        absolute=_probe(status=200, length=0, ctype="text/html"),
        prefixed=_probe(status=200, length=256, ctype="application/json"),
    )
    assert rep.api_findings[0].mismatch is True
