"""issue #40 收尾回归：MCP 接入可发现性（问题 1）与参数名契约提示（问题 3）。

- 问题 1：`lwa mcp` fatal 退出前向 stderr 写机器可解析的 ``lwaMcpFatal``
  JSON 行（MCP 客户端默认不展示人类可读 stderr，但日志可 grep）；
  doctor 例行巡检新增 ``mcp_dependency``（WARN 级）。
- 问题 3：契约 ``instanceId`` 字段带显式 description（标准名 + 取值来源），
  OpenAPI requestBody 与 MCP input_schema 双通道自动携带。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner


def _stderr_json_lines(output: str) -> list[dict]:
    """从 stderr 输出提取所有可解析的 JSON 行（lwaMcpFatal 诊断行）。"""
    lines = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "lwaMcpFatal" in parsed:
            lines.append(parsed)
    return lines


def _invoke_mcp(args: list[str]):
    from local_webpage_access.cli import app

    # click>=8.2 无 mix_stderr（stderr 默认并入 output）
    return CliRunner().invoke(app, ["mcp", *args])


def test_mcp_missing_dependency_emits_parseable_fatal(monkeypatch, tmp_path: Path) -> None:
    """问题 1：缺 [mcp] extra 时 stderr 含 lwaMcpFatal JSON 行 + 人类可读文本。"""
    from local_webpage_access.cli import mcp as mcp_cmd

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "local-web.yml").write_text("managerPort: 17899\n", encoding="utf-8")
    monkeypatch.setattr(mcp_cmd, "_sdk_available", lambda: False)

    result = _invoke_mcp(["--workspace", str(ws)])
    assert result.exit_code == 1
    fatals = _stderr_json_lines(result.output)
    assert len(fatals) == 1, "应有且仅有一行机器可解析诊断"
    assert fatals[0]["lwaMcpFatal"] == "missing-dependency"
    assert fatals[0]["hint"] == "pip install 'local-webpage-access[mcp]'"
    # 人类可读文本同场（客户端 UI 升级后可直接展示）
    assert "缺少 MCP SDK" in result.output


def test_mcp_workspace_fatal_codes(monkeypatch, tmp_path: Path) -> None:
    """问题 1：工作区两类 fatal 也带结构化诊断（not-absolute / invalid）。"""
    from local_webpage_access.cli import mcp as mcp_cmd

    monkeypatch.setattr(mcp_cmd, "_sdk_available", lambda: True)

    result = _invoke_mcp(["--workspace", "relative/path"])
    assert result.exit_code == 1
    assert _stderr_json_lines(result.output)[0]["lwaMcpFatal"] == "workspace-not-absolute"

    result = _invoke_mcp(["--workspace", str(tmp_path / "nope")])
    assert result.exit_code == 1
    assert _stderr_json_lines(result.output)[0]["lwaMcpFatal"] == "workspace-invalid"


def test_doctor_reports_mcp_dependency(monkeypatch) -> None:
    """问题 1：doctor mcp_dependency——装了 OK，缺了 WARN + 修复命令。"""
    import importlib.util

    from local_webpage_access.doctor import check_mcp_dependency

    result = check_mcp_dependency()
    if importlib.util.find_spec("mcp") is not None:
        assert result.status == "ok"
    else:  # pragma: no cover - 测试环境通常已装
        assert result.status == "warn"

    # 模拟缺依赖：find_spec("mcp") → None（其余模块不受影响）
    original = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "mcp" else original(name),
    )
    result = check_mcp_dependency()
    assert result.status == "warn"
    assert "local-webpage-access[mcp]" in (result.suggestion or "") + result.message


def test_instance_id_contract_documents_standard_name() -> None:
    """问题 3：instanceId 字段 description 说明标准名与取值来源（双通道携带）。"""
    from local_webpage_access.agent.contracts import (
        TOOL_SPECS,
        GetAccessUrlsInput,
        GetInstanceInput,
        GetLogsInput,
        LifecycleInput,
    )
    from local_webpage_access.mcp.server import tool_to_mcp

    for model in (GetInstanceInput, GetAccessUrlsInput, LifecycleInput):
        desc = model.model_json_schema()["properties"]["instanceId"]["description"]
        assert "instanceId" in desc and "lwa_list_instances" in desc

    logs_desc = GetLogsInput.model_json_schema()["properties"]["instanceId"]["description"]
    assert "二选一" in logs_desc

    # MCP input_schema 同源携带（agent 在 tools/list 即可看到，不必试错）
    for name in ("lwa_get_instance", "lwa_get_access_urls", "lwa_stop_instance"):
        spec = [s for s in TOOL_SPECS.values() if s.name == name][0]
        schema = tool_to_mcp(spec).input_schema
        assert "标准名" in schema["properties"]["instanceId"]["description"], name


@pytest.mark.parametrize("bad", ["instance", "id"])
def test_strict_contract_still_rejects_variants(bad: str) -> None:
    """问题 3 对照：description 提示不放松严格契约——变体仍被拒并指出正确字段。"""
    from pydantic import ValidationError

    from local_webpage_access.agent.contracts import GetInstanceInput

    with pytest.raises(ValidationError) as exc:
        GetInstanceInput.model_validate({bad: "demo"})
    issues = exc.value.errors(include_url=False)
    assert any(i.get("loc") == ["instanceId"] or i.get("type") == "missing" for i in issues)
