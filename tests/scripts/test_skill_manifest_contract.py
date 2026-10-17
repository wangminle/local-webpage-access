"""DOC-198 / DOC-199：内置技能的 manifest 取值和生成物指引必须与当前产品一致。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from local_webpage_access.models import InstanceManifest

_ROOT = Path(__file__).resolve().parents[2]
_SKILLS = _ROOT / "src" / "local_webpage_access" / "skills"

_BASE_MANIFEST = {
    "id": "demo",
    "name": "Demo",
    "kind": "static",
    "runtime": "shared-static",
    "servingMode": "shared-static",
    "status": "stopped",
    "desiredState": "stopped",
    "version": "1",
}


def _json_blocks(text: str) -> list[dict]:
    blocks = re.findall(r"```json\n(.*?)```", text, flags=re.S)
    return [json.loads(block) for block in blocks]


def _deep_merge(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def test_detect_stack_examples_validate() -> None:
    """技能示例合并进合法 manifest 后必须能通过当前 schema。"""
    text = (_SKILLS / "lwa-detect-stack" / "SKILL.md").read_text(encoding="utf-8")
    assert "form" in text
    assert "entry.start" in text or "`entry`" in text
    examples = _json_blocks(text)
    assert examples, "lwa-detect-stack 应给出可校验的 JSON 示例"
    for example in examples:
        payload = _deep_merge(_BASE_MANIFEST, example)
        if payload.get("runtime") == "docker-compose":
            payload.setdefault(
                "container",
                {
                    "projectName": "lwa-demo",
                    "internalPort": 3000,
                    "composePath": "docker/compose.yaml",
                    "dockerfilePath": "docker/Dockerfile",
                },
            )
        InstanceManifest.model_validate(payload)
    assert "设 `unknown`" not in text


def test_frontend_skill_serving_mode_is_schema_value() -> None:
    text = (_SKILLS / "lwa-build-frontend-static" / "SKILL.md").read_text(encoding="utf-8")
    for example in _json_blocks(text):
        InstanceManifest.model_validate(_deep_merge(_BASE_MANIFEST, example))


def test_generated_artifact_skills_do_not_treat_hand_edits_as_durable() -> None:
    """Dockerfile / compose 会按 manifest 重渲染，技能不能把手改生成物当成持久方案。"""
    names = (
        "lwa-fix-docker-build-failure",
        "lwa-fix-container-startup-failure",
        "lwa-generate-compose",
        "lwa-dockerize-node-app",
        "lwa-dockerize-python-app",
        "lwa-dockerize-fullstack-sqlite",
    )
    for name in names:
        text = (_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert "重新渲染" in text or "重新生成" in text, name
        assert "systemDeps" in text or "entry.start" in text or "entry" in text, name
    compose = (_SKILLS / "lwa-generate-compose" / "SKILL.md").read_text(encoding="utf-8")
    assert "在基线上人工追加" not in compose
    assert "不要往生成文件里人工追加" in compose
    readme = (_SKILLS / "README.md").read_text(encoding="utf-8")
    assert "重新渲染" in readme or "重新生成" in readme


@pytest.mark.parametrize("color", [False, True], ids=["plain", "ansi"])
def test_fix_build_skill_configure_example_is_a_real_option(monkeypatch, color: bool) -> None:
    """DOC-200：构建修复技能里的 configure 参数必须是已注册选项，不能是 --system-dep。"""
    from click import unstyle
    from typer import rich_utils
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    # GitHub Actions 会启用 Rich 终端样式；本机默认 NO_COLOR 时也要覆盖该分支。
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", color)

    text = (_SKILLS / "lwa-fix-docker-build-failure" / "SKILL.md").read_text(encoding="utf-8")
    assert re.search(r"--system-dep(?!s)", text) is None
    assert "--system-deps python3 --system-deps make --system-deps g++" in text
    configure_lines = [
        line for line in text.splitlines() if re.search(r"lwa configure\s+\S", line)
    ]
    assert configure_lines
    flags = {
        flag
        for line in configure_lines
        for flag in re.findall(r"--[a-z0-9]+(?:-[a-z0-9]+)*", line)
    }
    assert flags == {"--system-deps"}
    result = CliRunner().invoke(app, ["configure", "--help"])
    assert result.exit_code == 0, result.output
    help_out = unstyle(result.output)
    assert help_out
    for flag in sorted(flags):
        assert flag in help_out, flag
    assert re.search(r"--system-dep(?!s)", help_out) is None


def test_illegal_serving_mode_still_rejected() -> None:
    """对照：form 分类名不能写入 servingMode。"""
    payload = _deep_merge(_BASE_MANIFEST, {"servingMode": "frontend-static"})
    with pytest.raises(Exception):
        InstanceManifest.model_validate(payload)
