"""健康探针语义（PLN-052 S1-04 / N2 + #50 前半 + BUG-769~772）。

- 用户显式声明（declared）的期望状态严格精确匹配（BUG-769）；只有自动探针
  （discovered/guessed）保留 200 哨兵的 2xx/3xx 宽松族（BUG-506）；
- declared/discovered 探针响应为 HTML 时不计为 API 证据（SPA 兜底页假绿）；
  HTML 不撤销就绪结果——servesApi 是全应用能力声明，不给单个探针制造非 HTML
  内容契约（BUG-772/779）；纯重定向指令（3xx）同样不计为 API 证据（BUG-780）；
- 源码发现深度放宽到 6、支持 public-health 等带前缀命名，零结果留 WARNING。
"""

from __future__ import annotations

import http.server
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tests.scripts.test_probe_semantics_batch2 import _manifest

from local_webpage_access import candidate_generator, hosting
from local_webpage_access.candidate_generator import (
    DeploymentCandidate,
    _HEALTH_PATH_RE,
    _discover_health_probes,
    log as cg_log,
)
from local_webpage_access.models import ProjectEvidence

# ---- 状态判定 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected", "lenient", "matched"),
    [
        (200, 200, False, True),
        (204, 200, False, False),  # BUG-769：用户显式声明 200 严格匹配
        (204, 200, True, True),  # BUG-506：自动探针保留 2xx/3xx 宽松族
        (302, 200, False, False),
        (302, 200, True, True),
        (500, 200, False, False),
        (200, 201, False, False),  # 声明 201 时 200 不再“一律通过”
        (201, 201, True, True),  # 非 200 声明值即使宽松族也精确匹配
        (200, 401, False, False),
        (401, 401, False, True),
        (401, None, False, False),
    ],
)
def test_status_matches_declared_strict_auto_lenient(
    code: int, expected, lenient: bool, matched: bool
) -> None:
    assert hosting._status_matches(code, expected, lenient_family=lenient) is matched


def test_is_html_content_type() -> None:
    assert hosting._is_html_content_type("text/html; charset=utf-8")
    assert hosting._is_html_content_type("application/xhtml+xml")
    assert not hosting._is_html_content_type("application/json")
    assert not hosting._is_html_content_type(None)


# ---- 真实 HTTP 服务上的 _probe_path ---------------------------------------------


class _RoutesHandler(http.server.BaseHTTPRequestHandler):
    routes = {
        "/app": (200, "text/html; charset=utf-8", b"<!doctype html><html></html>"),
        "/api/health": (200, "application/json", b'{"ok":true}'),
        "/api/private": (401, "application/json", b'{"detail":"auth"}'),
    }

    def do_GET(self) -> None:  # noqa: N802
        # mark_probe_url 会附加探测标记查询串，匹配前去掉
        path = self.path.split("?", 1)[0]
        code, ctype, body = self.routes.get(path, (404, "text/plain", b"nf"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D401
        return


@pytest.fixture()
def route_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RoutesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_probe_path_returns_content_type_for_html_and_json(route_server: int) -> None:
    assert hosting._probe_path(route_server, "/api/health") == (
        True,
        200,
        "application/json",
    )
    passed, code, ctype = hosting._probe_path(route_server, "/app")
    assert (passed, code) == (True, 200)
    assert hosting._is_html_content_type(ctype)


def test_probe_path_declared_401_is_exact_match(route_server: int) -> None:
    passed, code, _ = hosting._probe_path(route_server, "/api/private", expected_status=401)
    assert (passed, code) == (True, 401)
    passed, code, _ = hosting._probe_path(route_server, "/api/private")
    assert (passed, code) == (False, 401)


def test_probe_path_connection_refused_returns_none_triplet() -> None:
    assert hosting._probe_path(1, "/x", timeout=0.2) == (False, None, None)


# ---- N2：HTML 不计为 API 证据（完整验证流程）------------------------------------


def _evaluate(monkeypatch, manifest, *, passed: bool, code: int, ctype: str | None) -> dict:
    monkeypatch.setattr(hosting, "_wait_for_http", lambda *a, **k: True)
    monkeypatch.setattr(hosting, "_probe_path", lambda *a, **k: (passed, code, ctype))
    return hosting._evaluate_container_verification(
        19999, manifest, MagicMock(), MagicMock(), manifest.id
    )


def test_discovered_probe_returning_html_is_not_api_evidence(monkeypatch) -> None:
    m = _manifest(
        probes=[{"path": "/api/v1/public-health", "isMandatory": False, "source": "discovered"}]
    )
    result = _evaluate(monkeypatch, m, passed=True, code=200, ctype="text/html; charset=utf-8")
    assert result["overall_status"] == "degraded"
    assert any("/api/v1/public-health 返回 HTML" in note for note in result["probe_notes"])


def test_discovered_probe_returning_json_still_counts(monkeypatch) -> None:
    m = _manifest(
        probes=[{"path": "/api/v1/public-health", "isMandatory": False, "source": "discovered"}]
    )
    result = _evaluate(monkeypatch, m, passed=True, code=200, ctype="application/json")
    assert result["overall_status"] == "passed"
    assert result["probe_notes"] == []


def test_declared_mandatory_probe_returning_html_keeps_readiness(monkeypatch) -> None:
    # BUG-772/779：servesApi 是全应用能力声明，不给单个探针制造非 HTML
    # 内容契约——mandatory 声明探针返回 HTML 时就绪结果保留，仅不计为
    # API 证据；无其他证据时整体降级为 degraded（证据缺失告警）。
    m = _manifest(
        probes=[{"path": "/ready", "isMandatory": True, "source": "declared"}],
        contract={
            "servesUi": True,
            "servesApi": True,
            "requiresDatabase": False,
            "requiresMigrations": False,
            "requiredProbes": [{"path": "/ready", "isMandatory": True, "source": "declared"}],
        },
    )
    result = _evaluate(monkeypatch, m, passed=True, code=200, ctype="text/html")
    assert result["overall_status"] == "degraded"
    assert result["mandatory_all_passed"] is True
    assert "api" not in result["observed_capabilities"]
    assert any("/ready 返回 HTML" in note for note in result["probe_notes"])


# ---- 源码发现：N2 / #50 前半 -----------------------------------------------------


def test_health_path_re_accepts_prefixed_health_names() -> None:
    for route in (
        "/health",
        "/healthz",
        "/api/v1/public-health",
        "/healthcheck",
        "/health-check",
        "/health_check",
        "/ping",
    ):
        assert _HEALTH_PATH_RE.search(route), route
    for route in ("/healthy-users", "/users", "/api/v1/members", "/unhealth-data"):
        assert not _HEALTH_PATH_RE.search(route), route


def _backend(kind: str = "python") -> DeploymentCandidate:
    return DeploymentCandidate(
        kind=kind,
        runtime="docker_compose",
        servingMode="container",
        form="backend-container",
    )


def test_discovery_reaches_routes_four_levels_deep(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "app.py").write_text(
        '@app.get("/api/v1/public-health")\ndef h():\n    return {}\n', encoding="utf-8"
    )
    probes = _discover_health_probes(ProjectEvidence(root=str(tmp_path)), _backend())
    assert [p.path for p in probes] == ["/api/v1/public-health"]
    assert probes[0].source == "discovered"
    assert probes[0].isMandatory is False


def _capture_warnings(fn) -> list[str]:
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler(level=logging.WARNING)
    cg_log.addHandler(handler)
    orig_level = cg_log.level
    cg_log.setLevel(logging.WARNING)
    try:
        fn()
    finally:
        cg_log.removeHandler(handler)
        cg_log.setLevel(orig_level)
    return [r.getMessage() for r in records if r.levelno == logging.WARNING]


def test_discovery_warns_when_no_route_declarations(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    messages = _capture_warnings(
        lambda: _discover_health_probes(ProjectEvidence(root=str(tmp_path)), _backend()),
    )
    assert any("解析到 0 条路由声明" in m and str(tmp_path) in m for m in messages)


def test_discovery_warns_when_file_cap_reached(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(candidate_generator, "_DISCOVERY_MAX_FILES", 2)
    for i in range(3):
        (tmp_path / f"m{i}.py").write_text(
            f'@app.get("/health{i}")\ndef h():\n    return {{}}\n', encoding="utf-8"
        )
    messages = _capture_warnings(
        lambda: _discover_health_probes(ProjectEvidence(root=str(tmp_path)), _backend()),
    )
    assert any("文件数上限 2" in m for m in messages)


def test_discovery_no_warning_when_routes_found(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(
        '@app.get("/health")\ndef h():\n    return {}\n', encoding="utf-8"
    )
    messages = _capture_warnings(
        lambda: _discover_health_probes(ProjectEvidence(root=str(tmp_path)), _backend()),
    )
    assert messages == []
