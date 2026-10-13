"""BUG-769~772/779/780 探针分层语义回归（复审 F08/F09/F10/F11 + CHK-380）。

用真实临时回环 HTTP 服务验证「状态门槛 / 内容契约 / API 证据」分层：
- BUG-769：declared 期望 200 严格匹配（204 不得通过）；discovered 自动探针
  保留 BUG-506 的 2xx/3xx 宽松族兼容；
- BUG-770：证据探针禁止自动重定向，按原端点响应（302）判定，最终响应不得
  冒充原路径 API 证据；
- BUG-771：错误响应保留 Content-Type（401 text/html 不得绕过过滤当 API 证据），
  HTTPError 正确关闭；
- BUG-772/779：HTML 不撤销就绪结果；servesApi 是全应用能力声明，不给单个
  探针制造非 HTML 内容契约——纯网页应用与混合 UI/API 应用的 HTML 首页
  mandatory 探针都是正确就绪结果；
- BUG-780：纯重定向指令（3xx 无内容）不计入 API 证据；显式期望 302 的
  就绪门槛仍可以满足。
"""

from __future__ import annotations

import http.server
import threading
import urllib.error
from email.message import Message
from unittest.mock import MagicMock

import pytest
from tests.scripts.test_probe_semantics_batch2 import _manifest

from local_webpage_access import hosting


class _RoutesHandler(http.server.BaseHTTPRequestHandler):
    routes = {
        "/": (200, "text/html", b"<html><body>Healthy UI</body></html>"),
        "/ok": (200, "application/json", b'{"ok":true}'),
        "/api/health": (200, "application/json", b'{"ok":true}'),
        "/unexpected": (204, "application/json", b""),
        "/private": (401, "text/html", b"<html>Sign in</html>"),
    }

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        code, ctype, body = self.routes.get(path, (404, "text/plain", b"Not found"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


@pytest.fixture()
def probe_server() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RoutesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _evaluate(port: int, manifest) -> dict:
    """只隔离无关的等待阶段，业务探针走真实本机 HTTP。"""
    orig = hosting._wait_for_http
    hosting._wait_for_http = lambda *a, **k: True
    try:
        return hosting._evaluate_container_verification(
            port, manifest, MagicMock(), MagicMock(), manifest.id
        )
    finally:
        hosting._wait_for_http = orig


# ---- BUG-769：declared 严格匹配，自动探针保留宽松族 -------------------------------


def test_declared_expected_200_rejects_204(probe_server: int) -> None:
    m = _manifest(
        probes=[{"path": "/unexpected", "expectedStatus": 200, "isMandatory": True, "source": "declared"}],
        contract={
            "servesUi": True,
            "servesApi": True,
            "requiresDatabase": False,
            "requiresMigrations": False,
            "requiredProbes": [
                {"path": "/unexpected", "expectedStatus": 200, "isMandatory": True, "source": "declared"}
            ],
        },
    )
    result = _evaluate(probe_server, m)
    assert result["overall_status"] == "failed"
    assert result["mandatory_all_passed"] is False
    assert "api" not in result["observed_capabilities"]


def test_discovered_probe_keeps_lenient_2xx_family(probe_server: int) -> None:
    """BUG-506 兼容策略：自动发现探针期望 200 时 204 仍视为通过并计为证据。"""
    m = _manifest(
        probes=[{"path": "/unexpected", "isMandatory": False, "source": "discovered"}],
        contract={
            "servesUi": True,
            "servesApi": True,
            "requiresDatabase": False,
            "requiresMigrations": False,
            "requiredProbes": [
                {"path": "/unexpected", "isMandatory": False, "source": "discovered"}
            ],
        },
    )
    result = _evaluate(probe_server, m)
    assert result["overall_status"] == "passed"
    assert "api" in result["observed_capabilities"]


# ---- BUG-770：证据探针禁止自动重定向 ------------------------------------------


def test_probe_path_does_not_follow_redirect(probe_server: int) -> None:
    passed, code, _ = hosting._probe_path(probe_server, "/redirect", expected_status=200)
    assert (passed, code) == (False, 302)
    # 用户明确期望重定向状态时按原响应 302 判定
    passed, code, _ = hosting._probe_path(probe_server, "/redirect", expected_status=302)
    assert (passed, code) == (True, 302)


def test_redirected_json_is_not_api_evidence(probe_server: int) -> None:
    m = _manifest(
        probes=[{"path": "/redirect", "isMandatory": True, "source": "declared"}],
        contract={
            "servesUi": True,
            "servesApi": True,
            "requiresDatabase": False,
            "requiresMigrations": False,
            "requiredProbes": [
                {"path": "/redirect", "isMandatory": True, "source": "declared"}
            ],
        },
    )
    result = _evaluate(probe_server, m)
    assert result["overall_status"] == "failed"
    assert "api" not in result["observed_capabilities"]


# ---- BUG-771：错误响应保留 Content-Type ---------------------------------------


def test_probe_path_keeps_content_type_on_http_error(probe_server: int) -> None:
    passed, code, ctype = hosting._probe_path(probe_server, "/private", expected_status=401)
    assert (passed, code) == (True, 401)
    assert ctype is not None and ctype.startswith("text/html")


def test_html_error_page_is_not_api_evidence(probe_server: int) -> None:
    """BUG-771/779：状态门槛（401 精确匹配）与 API 证据分层——HTML 错误页
    满足就绪门槛但不得计入 API 证据，整体降级为 degraded（证据缺失告警）。"""
    m = _manifest(
        probes=[{"path": "/private", "expectedStatus": 401, "isMandatory": True, "source": "declared"}],
        contract={
            "servesUi": True,
            "servesApi": True,
            "requiresDatabase": False,
            "requiresMigrations": False,
            "requiredProbes": [
                {"path": "/private", "expectedStatus": 401, "isMandatory": True, "source": "declared"}
            ],
        },
    )
    result = _evaluate(probe_server, m)
    assert result["overall_status"] == "degraded"
    assert result["mandatory_all_passed"] is True
    assert "api" not in result["observed_capabilities"]
    assert any("/private 返回 HTML" in note for note in result["probe_notes"])


def test_probe_path_closes_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    err = urllib.error.HTTPError(
        "http://127.0.0.1:1/x", 401, "Unauthorized", Message(), None
    )
    err.hdrs["Content-Type"] = "text/html"
    err.close = MagicMock()

    def _raise(url, *, timeout=None):
        raise err

    monkeypatch.setattr(hosting, "urlopen_no_redirect", _raise)
    assert hosting._probe_path(1, "/x", expected_status=401) == (True, 401, "text/html")
    err.close.assert_called_once()


# ---- BUG-772：就绪门槛与 API 证据分层 ------------------------------------------


def test_ui_only_app_html_200_passes_readiness_gate(probe_server: int) -> None:
    """纯网页应用（servesUi=True/servesApi=False）：用户声明 / expected=200，
    真实返回 200 text/html 是正确就绪结果，不得触发 mandatory 失败回滚。"""
    m = _manifest(
        contract={
            "servesUi": True,
            "servesApi": False,
            "requiresDatabase": False,
            "requiresMigrations": False,
            "requiredProbes": [],
        },
        overrides={"probes": [{"path": "/", "expectedStatus": 200}]},
    )
    result = _evaluate(probe_server, m)
    assert result["overall_status"] == "passed"
    assert result["mandatory_all_passed"] is True


# ---- BUG-779：混合 UI/API 应用的 HTML 首页不误判 mandatory 失败 -------------


def _mixed_manifest(api_probe: dict):
    """混合应用：servesUi+servesApi，首页用户声明 mandatory 探针 + API 探针。"""
    return _manifest(
        contract={
            "servesUi": True,
            "servesApi": True,
            "requiresDatabase": False,
            "requiresMigrations": False,
            "requiredProbes": [api_probe],
        },
        overrides={"probes": [{"path": "/", "expectedStatus": 200}]},
    )


@pytest.mark.parametrize(
    "api_probe",
    [
        {"path": "/api/health", "isMandatory": False, "source": "discovered"},
        {"path": "/api/health", "isMandatory": False, "source": "declared"},
        {"path": "/api/health", "isMandatory": True, "source": "declared"},
    ],
)
def test_mixed_app_homepage_html_readiness_passes(probe_server: int, api_probe: dict) -> None:
    """首页 200 HTML 满足用户声明的就绪门槛；API 探针 200 JSON 提供证据——
    三种 API 探针配置下整体都必须通过，不得因首页 HTML 判 failed。"""
    result = _evaluate(probe_server, _mixed_manifest(api_probe))
    assert result["overall_status"] == "passed"
    assert result["mandatory_all_passed"] is True
    assert result["observed_capabilities"] == ["api", "ui"]


# ---- BUG-780：纯重定向指令不计入 API 证据 ------------------------------------


def test_empty_redirect_is_not_api_evidence(probe_server: int) -> None:
    """discovered 探针期望 200、端点仅返回 302 + Location：宽松族可达性兼容
    保留，但跳转指令无实质内容，不得计为已观测 API 能力。"""
    m = _manifest(
        probes=[{"path": "/redirect", "expectedStatus": 200, "isMandatory": False, "source": "discovered"}],
        contract={
            "servesUi": True,
            "servesApi": True,
            "requiresDatabase": False,
            "requiresMigrations": False,
            "requiredProbes": [
                {"path": "/redirect", "expectedStatus": 200, "isMandatory": False, "source": "discovered"}
            ],
        },
    )
    result = _evaluate(probe_server, m)
    assert "api" not in result["observed_capabilities"]
    assert result["observed_capabilities"] == ["ui"]
    assert result["overall_status"] == "degraded"


def test_declared_expected_302_satisfies_readiness_gate(probe_server: int) -> None:
    """显式期望 302 的用户就绪门槛仍按原响应满足；302 同样不提供 API 证据。"""
    m = _manifest(
        probes=[{"path": "/redirect", "expectedStatus": 302, "isMandatory": True, "source": "declared"}],
        contract={
            "servesUi": True,
            "servesApi": True,
            "requiresDatabase": False,
            "requiresMigrations": False,
            "requiredProbes": [
                {"path": "/redirect", "expectedStatus": 302, "isMandatory": True, "source": "declared"}
            ],
        },
    )
    result = _evaluate(probe_server, m)
    assert result["mandatory_all_passed"] is True
    assert "api" not in result["observed_capabilities"]


# ---- guessed 探针通过不告警、不降级（复查发现的中等缺陷）--------------------


def test_guessed_probe_pass_does_not_warn_or_degrade(probe_server: int) -> None:
    """guessed 探针（自动 /health 诊断）本就不具备 API 证据资格，通过后不得
    报「未计入 API 证据」告警——否则默认配置下健康应用由 passed 误判 degraded。"""
    m = _manifest(
        contract={
            "servesUi": True,
            "servesApi": False,
            "requiresDatabase": False,
            "requiresMigrations": False,
            "requiredProbes": [
                {"path": "/ok", "isMandatory": False, "source": "guessed"},
            ],
        },
        overrides={"probes": [{"path": "/", "expectedStatus": 200}]},
    )
    result = _evaluate(probe_server, m)
    assert result["overall_status"] == "passed"
    assert result["optional_warnings"] == []
