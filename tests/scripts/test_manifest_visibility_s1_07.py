"""小型可见性修复（PLN-052 S1-07 / #56-3）。

manifest 损坏时 status 不得继续显示默认来源类型 ``zip``；错误摘要须可见且同一错误只告警一次。
"""

from __future__ import annotations

from local_webpage_access import status as status_mod
from local_webpage_access.cli.status import _echo_source_lines
from local_webpage_access.status import (
    _MANIFEST_ERROR_LOGGED,
    _manifest_error_summary,
    instance_status,
)
from tests.scripts.test_health_status import _seed_static


def test_corrupt_manifest_is_reported_not_defaulted_to_zip(workspace, registry, config) -> None:
    _seed_static(workspace, registry, "demo", host_port=21100)
    workspace.app_manifest_path("demo").write_text("{not json", encoding="utf-8")

    st = instance_status(workspace, config, registry, "demo")

    assert st.manifest_error
    assert st.source_kind is None
    payload = st.to_dict()
    assert payload["manifestError"] == st.manifest_error
    assert payload["sourceKind"] is None


def test_healthy_manifest_has_no_manifest_error(workspace, registry, config) -> None:
    _seed_static(workspace, registry, "demo", host_port=21100)

    st = instance_status(workspace, config, registry, "demo")

    assert st.manifest_error is None
    assert st.to_dict()["manifestError"] is None


def test_cli_shows_manifest_read_failure_instead_of_zip(capsys) -> None:
    class _Status:
        port_mapping_label = None
        source_kind = None
        manifest_error = "JSONDecodeError: Expecting value"

    _echo_source_lines(_Status())

    out = capsys.readouterr().out
    assert "manifest 读取失败" in out
    assert "zip" not in out


def test_manifest_error_warning_is_rate_limited(monkeypatch) -> None:
    _MANIFEST_ERROR_LOGGED.clear()
    warnings: list[tuple] = []
    monkeypatch.setattr(status_mod.log, "warning", lambda *a, **k: warnings.append(a))

    exc = ValueError("bad manifest")
    first = _manifest_error_summary("rate-limited", exc)
    second = _manifest_error_summary("rate-limited", exc)

    assert first == second == "ValueError: bad manifest"
    assert len(warnings) == 1
    _MANIFEST_ERROR_LOGGED.clear()
