"""security 模块测试（WBS-25 安全、权限与默认保护）。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from local_webpage_access.security import (
    LEVEL_CRITICAL,
    LEVEL_INFO,
    LEVEL_WARN,
    SecurityError,
    SecurityFinding,
    assert_no_critical,
    audit_compose,
    audit_dockerfile,
    audit_zip_members,
    has_critical,
    trusted_zip_hint,
    unknown_zip_risk_hint,
    validate_manager_binding,
)


# ---- 辅助 -----------------------------------------------------------------


def _codes(findings: list[SecurityFinding]) -> list[str]:
    return [f.code for f in findings]


def _critical_codes(findings: list[SecurityFinding]) -> list[str]:
    return [f.code for f in findings if f.level == LEVEL_CRITICAL]


# ---- SecurityFinding -----------------------------------------------------


def test_finding_to_dict_with_detail() -> None:
    f = SecurityFinding(LEVEL_WARN, "x", "msg", detail="d")
    assert f.to_dict() == {
        "level": "warn",
        "code": "x",
        "message": "msg",
        "detail": "d",
    }


def test_finding_to_dict_without_detail() -> None:
    f = SecurityFinding(LEVEL_INFO, "x", "msg")
    assert f.to_dict() == {"level": "info", "code": "x", "message": "msg"}


def test_finding_is_frozen() -> None:
    f = SecurityFinding(LEVEL_INFO, "x", "msg")
    with pytest.raises(Exception):  # dataclass(frozen=True)
        f.level = "warn"  # type: ignore[misc]


# ---- audit_compose -------------------------------------------------------


_CLEAN_COMPOSE = """\
name: demo
services:
  app:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    ports:
      - "${HOST_PORT}:${INTERNAL_PORT}"
    volumes:
      - ../data:/app/data
    restart: unless-stopped
"""


def test_audit_compose_clean_template_no_critical() -> None:
    findings = audit_compose(_CLEAN_COMPOSE)
    assert _critical_codes(findings) == []


def test_audit_compose_detects_privileged() -> None:
    text = _CLEAN_COMPOSE.replace(
        "    restart: unless-stopped\n", "    privileged: true\n    restart: unless-stopped\n"
    )
    findings = audit_compose(text)
    assert "privileged" in _critical_codes(findings)


def test_audit_compose_detects_docker_socket_short_form() -> None:
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - /var/run/docker.sock:/var/run/docker.sock",
    )
    findings = audit_compose(text)
    assert "docker_socket_mount" in _critical_codes(findings)


def test_audit_compose_detects_docker_socket_long_form() -> None:
    text = """\
services:
  app:
    image: x
    volumes:
      - type: bind
        source: /var/run/docker.sock
        target: /var/run/docker.sock
"""
    findings = audit_compose(text)
    assert "docker_socket_mount" in _critical_codes(findings)


def test_audit_compose_detects_host_sensitive_root() -> None:
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - /etc:/etc",
    )
    findings = audit_compose(text)
    assert "host_sensitive_mount" in _critical_codes(findings)


def test_audit_compose_detects_host_sensitive_nested() -> None:
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - /var/lib/docker:/host-docker",
    )
    findings = audit_compose(text)
    assert "host_sensitive_mount" in _critical_codes(findings)


def test_audit_compose_detects_dict_volume_host_sensitive() -> None:
    """BUG-032：Compose dict 格式 volumes 的宿主路径也要审计。"""
    text = """\
services:
  app:
    image: x
    volumes:
      /app/data: /etc/passwd
"""
    findings = audit_compose(text)
    assert "host_sensitive_mount" in _critical_codes(findings)


def test_audit_compose_detects_windows_drive_sensitive_mount() -> None:
    """BUG-042：Windows 盘符 bind mount 不应被当作命名卷放行。"""
    text = """\
services:
  app:
    image: x
    volumes:
      - C:\\Users:/app/host
"""
    findings = audit_compose(text)
    assert "host_sensitive_mount" in _critical_codes(findings)


def test_audit_compose_allows_named_volume() -> None:
    text = """\
services:
  app:
    image: x
    volumes:
      - app_data:/app/data
volumes:
  app_data: {}
"""
    findings = audit_compose(text)
    # 命名卷不应触发 critical
    assert _critical_codes(findings) == []


def test_audit_compose_warns_unexpected_relative_mount() -> None:
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - ../secrets:/run/secrets",
    )
    findings = audit_compose(text)
    assert "unexpected_host_mount" in _codes(findings)
    assert "unexpected_host_mount" not in _critical_codes(findings)


def test_audit_compose_warns_host_network() -> None:
    text = _CLEAN_COMPOSE.replace(
        "    restart: unless-stopped\n",
        "    network_mode: host\n    restart: unless-stopped\n",
    )
    findings = audit_compose(text)
    assert "host_network" in _codes(findings)
    assert "host_network" not in _critical_codes(findings)


def test_audit_compose_warns_dangerous_capability() -> None:
    text = _CLEAN_COMPOSE.replace(
        "    restart: unless-stopped\n",
        "    cap_add: [SYS_ADMIN]\n    restart: unless-stopped\n",
    )
    findings = audit_compose(text)
    assert "dangerous_capability" in _codes(findings)


def test_audit_compose_warns_root_user() -> None:
    text = _CLEAN_COMPOSE.replace(
        "    restart: unless-stopped\n",
        "    user: root\n    restart: unless-stopped\n",
    )
    findings = audit_compose(text)
    assert "root_user" in _codes(findings)


def test_audit_compose_invalid_yaml() -> None:
    findings = audit_compose("services: [this is not : valid yaml")
    assert "invalid_yaml" in _critical_codes(findings)


def test_audit_compose_not_a_mapping() -> None:
    findings = audit_compose("- just\n- a\n- list\n")
    assert "invalid_yaml" in _critical_codes(findings)


def test_audit_compose_custom_allowed_mounts() -> None:
    text = """\
services:
  app:
    image: x
    volumes:
      - ./uploads:/app/uploads
"""
    # 默认不允许 ./uploads
    findings = audit_compose(text)
    assert "unexpected_host_mount" in _codes(findings)
    # 扩展白名单后应放行
    findings2 = audit_compose(
        text, allowed_host_mounts=frozenset({"./data", "../data", "./uploads"})
    )
    assert "unexpected_host_mount" not in _codes(findings2)


# ---- audit_dockerfile ----------------------------------------------------


def test_audit_dockerfile_clean_with_user() -> None:
    text = textwrap.dedent(
        """\
        FROM node:20-alpine
        WORKDIR /app
        COPY . .
        USER node
        CMD ["node", "server.js"]
        """
    )
    findings = audit_dockerfile(text)
    codes = _codes(findings)
    assert "no_user" not in codes
    assert "root_user" not in codes


def test_audit_dockerfile_no_user_info() -> None:
    text = "FROM python:3.13\nCMD [\"python\"]\n"
    findings = audit_dockerfile(text)
    assert "no_user" in _codes(findings)
    assert "no_user" not in _critical_codes(findings)


def test_audit_dockerfile_explicit_root_warns() -> None:
    text = "FROM python:3.13\nUSER root\nCMD [\"python\"]\n"
    findings = audit_dockerfile(text)
    assert "root_user" in _codes(findings)


def test_audit_dockerfile_add_remote_url() -> None:
    text = (
        "FROM alpine\n"
        'ADD https://example.com/file.tar.gz /tmp/file.tar.gz\n'
        'CMD ["sh"]\n'
    )
    findings = audit_dockerfile(text)
    assert "add_remote_url" in _codes(findings)


def test_audit_dockerfile_pipe_to_shell() -> None:
    text = (
        "FROM alpine\n"
        "RUN curl -fsSL https://get.docker.com | sh\n"
        'CMD ["sh"]\n'
    )
    findings = audit_dockerfile(text)
    assert "pipe_to_shell" in _codes(findings)


def test_audit_dockerfile_copy_is_ok() -> None:
    text = "FROM alpine\nCOPY package.json .\nUSER nobody\nCMD [\"sh\"]\n"
    findings = audit_dockerfile(text)
    assert "add_remote_url" not in _codes(findings)


# ---- audit_zip_members ---------------------------------------------------


def test_audit_zip_members_clean() -> None:
    findings = audit_zip_members(["index.html", "css/style.css", "js/app.js"])
    assert findings == []


def test_audit_zip_members_absolute_path() -> None:
    findings = audit_zip_members(["/etc/passwd", "index.html"])
    assert "zip_absolute_path" in _critical_codes(findings)


def test_audit_zip_members_drive_letter() -> None:
    findings = audit_zip_members(["C:/Windows/System32/evil.dll"])
    assert "zip_drive_letter" in _critical_codes(findings)


def test_audit_zip_members_traversal_escape() -> None:
    findings = audit_zip_members(["../../../etc/passwd", "app/index.html"])
    assert "zip_slip" in _critical_codes(findings)


def test_audit_zip_members_traversal_within_is_ok() -> None:
    # 在 zip 根范围内的 .. 不会逃逸：a/../b == b
    findings = audit_zip_members(["a/../b.txt", "x/y/../../z.txt"])
    assert findings == []


def test_audit_zip_members_windows_backslash_traversal() -> None:
    findings = audit_zip_members(["..\\..\\evil.txt"])
    assert "zip_slip" in _critical_codes(findings)


# ---- 风险提示（WBS-25.09）-------------------------------------------------


def test_unknown_zip_risk_hint_nonempty() -> None:
    hint = unknown_zip_risk_hint()
    assert isinstance(hint, str)
    assert len(hint) > 20
    assert "pending" in hint or "未识别" in hint or "风险" in hint


def test_trusted_zip_hint_nonempty() -> None:
    hint = trusted_zip_hint()
    assert isinstance(hint, str)
    assert len(hint) > 10


def test_unknown_and_trusted_hints_differ() -> None:
    assert unknown_zip_risk_hint() != trusted_zip_hint()


# ---- 管理页绑定策略（WBS-25.02）-------------------------------------------


def test_validate_binding_loopback_ok() -> None:
    findings = validate_manager_binding("127.0.0.1", has_token=True)
    assert findings == []


def test_validate_binding_loopback_ok_without_token() -> None:
    # 回环地址无 token 也不算 critical（本机自用）
    findings = validate_manager_binding("127.0.0.1", has_token=False)
    assert _critical_codes(findings) == []


def test_validate_binding_lan_with_token_info() -> None:
    findings = validate_manager_binding("0.0.0.0", has_token=True, port=17800)
    assert any(f.level == LEVEL_INFO for f in findings)
    assert _critical_codes(findings) == []


def test_validate_binding_lan_without_token_critical() -> None:
    findings = validate_manager_binding("0.0.0.0", has_token=False)
    assert "lan_bind_no_token" in _critical_codes(findings)


def test_validate_binding_localhost_string() -> None:
    findings = validate_manager_binding("localhost", has_token=False)
    assert findings == []


# ---- 聚合校验 -------------------------------------------------------------


def test_has_critical_true_false() -> None:
    assert has_critical(
        [SecurityFinding(LEVEL_CRITICAL, "x", "y")]
    )
    assert not has_critical(
        [SecurityFinding(LEVEL_WARN, "x", "y")]
    )


def test_assert_no_critical_passes_when_clean() -> None:
    assert_no_critical([SecurityFinding(LEVEL_WARN, "x", "y")])
    assert_no_critical([])


def test_assert_no_critical_raises_on_critical() -> None:
    findings = [SecurityFinding(LEVEL_CRITICAL, "privileged", "boom")]
    with pytest.raises(SecurityError) as exc_info:
        assert_no_critical(findings)
    assert exc_info.value.findings == findings


# ---- 集成：生成的 compose 通过审计（WBS-25.03/04/05）-----------------------

def test_generated_compose_passes_audit(tmp_path: Path) -> None:
    """generate_compose 产出的 compose.yaml 不得含 critical 安全问题。"""
    from tests._helpers import make_container_manifest

    from local_webpage_access.compose import generate_compose
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path / "ws")
    ws.ensure_workspace_dirs()
    manifest = make_container_manifest("secure-demo")
    out = generate_compose(manifest, ws, host_port=18100)
    text = out.read_text(encoding="utf-8")
    findings = audit_compose(text)
    assert _critical_codes(findings) == [], (
        f"生成的 compose 含 critical 问题：{_critical_codes(findings)}"
    )


# ---- 集成：importer 对 pending 实例写风险提示（WBS-25.09）-----------------

def test_importer_writes_risk_hint_for_pending(tmp_path: Path, monkeypatch) -> None:
    """detection.pending 时应额外写一条 security 事件，含风险提示。"""
    from local_webpage_access.config import example_config_text, load_config
    from local_webpage_access.importer import Importer
    from local_webpage_access.paths import Workspace
    from local_webpage_access.registry import Registry

    ws = Workspace(tmp_path / "ws")
    ws.ensure_workspace_dirs()
    ws.config_path.write_text(example_config_text(), encoding="utf-8")
    config = load_config(ws)

    # 构造一个会让 detection.pending=True 的 zip
    zip_path = tmp_path / "mystery.zip"
    _make_zip(zip_path, {"readme.txt": "no recognizable stack"})

    reg = Registry(ws.db_path)
    reg.open()
    try:
        importer = Importer(ws, config, reg)
        result = importer.import_zip(zip_path)
        events = reg.list_events(result.instance_id)
        security_events = [e for e in events if e["event_type"] == "security"]
        assert len(security_events) >= 1, [e["event_type"] for e in events]
        assert (
            "风险" in security_events[0]["message"]
            or "供应链" in security_events[0]["message"]
        )
    finally:
        reg.close()


def _make_zip(zip_path: Path, files: dict[str, str]) -> None:
    import zipfile

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
