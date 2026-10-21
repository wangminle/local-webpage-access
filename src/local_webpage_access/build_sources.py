"""构建下载源健康、缓存通道、日志分段与代理诊断（CHK-422/426/427，DEV-145/147/148）。

LWA 不改用户网络，只记录失败/冷却、把缓存放到工作区而不是 current/，
并在手动构建进行时让自愈让路。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from local_webpage_access.logging import get_logger
from local_webpage_access.paths import Workspace

log = get_logger("build_sources")

_DEFAULT_COOLDOWN_SEC = 15 * 60
_OWNER_STALE_SEC = 3600
_HEALTH_FILE = "source-health.json"
_OWNER_FILE = "build-owner.json"
_REGISTRY_HOST_RE = re.compile(
    r"(?:^|[\s\"'])((?:https?://)?(?:ghcr\.io|docker\.io|registry-1\.docker\.io|"
    r"quay\.io|gcr\.io|mcr\.microsoft\.com|registry\.npmjs\.org|registry\.yarnpkg\.com|"
    r"registry\.npmmirror\.com|pypi\.org|files\.pythonhosted\.org)[^\\s\"']*)",
    re.IGNORECASE,
)


def pkg_cache_dir(workspace: Workspace, manager: str) -> Path:
    """独立于 current/ 的包管理器缓存目录（CHK-426）。"""
    safe = re.sub(r"[^a-z0-9]+", "-", manager.lower()).strip("-") or "npm"
    path = workspace.run / "pkg-cache" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def inject_pkg_cache_env(env: dict[str, str], workspace: Workspace, manager: str) -> dict[str, str]:
    """把缓存指到工作区通道，避免 import/update 重建 current/ 时丢掉预热。"""
    out = dict(env)
    cache = str(pkg_cache_dir(workspace, manager))
    name = manager.lower()
    if name in {"npm", "node"}:
        out.setdefault("npm_config_cache", cache)
    if name in {"yarn", "node"}:
        out.setdefault("YARN_CACHE_FOLDER", cache)
        out.setdefault("YARN_ENABLE_GLOBAL_CACHE", "1")
    if name in {"pnpm", "node"}:
        out.setdefault("PNPM_STORE_DIR", cache)
        out.setdefault("npm_config_store_dir", cache)
    if name == "bun":
        out.setdefault("BUN_INSTALL_CACHE_DIR", cache)
    if name == "node":
        out.setdefault("npm_config_cache", str(pkg_cache_dir(workspace, "npm")))
    return out


def write_build_segment(
    fh,
    *,
    instance_id: str,
    attempt: str,
    command: str,
    owner: str = "system",
) -> None:
    """每次构建追加分段头，不截断历史（CHK-427）。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    fh.write(
        "\n"
        f"======== LWA-BUILD {now} instance={instance_id} "
        f"attempt={attempt} owner={owner} ========\n"
        f"$ {command}\n"
    )
    fh.flush()


def host_build_timeout(config: Any, *, monorepo: bool = False) -> int:
    """宿主构建超时预算（CHK-422）。monorepo 首次 install 给更长窗口。"""
    base = int(getattr(config, "hostBuildTimeout", 1800) or 1800)
    base = max(60, min(base, 7200))
    if monorepo:
        return min(7200, max(base, 1800))
    return base


def timeout_hint(timeout: int, manager: str | None = None) -> str:
    pm = manager or "包管理器"
    return (
        f"命令超时（{timeout}s）。网络抖动时请重试：缓存已独立于 current/，"
        f"不必重装全部依赖。可在 local-web.yml 提高 hostBuildTimeout，"
        f"或先检查 {pm} registry 健康度。"
    )


def _health_path(workspace: Workspace) -> Path:
    return workspace.run / _HEALTH_FILE


def load_source_health(workspace: Workspace) -> dict[str, Any]:
    path = _health_path(workspace)
    if not path.is_file():
        return {"sources": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sources": {}}
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return {"sources": {}}
    return data


def record_source_failure(
    workspace: Workspace,
    source: str,
    *,
    error: str,
    cooldown_sec: int = _DEFAULT_COOLDOWN_SEC,
) -> dict[str, Any]:
    """按源记录失败与冷却，不因随后一次成功就断言竞态结束（DEV-145）。"""
    key = _source_key(source)
    data = load_source_health(workspace)
    sources = data.setdefault("sources", {})
    now = datetime.now(timezone.utc)
    prev = sources.get(key) if isinstance(sources.get(key), dict) else {}
    failures = int(prev.get("failures") or 0) + 1
    entry = {
        "source": key,
        "failures": failures,
        "lastError": error[:500],
        "lastFailureAt": now.isoformat(),
        "cooldownUntil": datetime.fromtimestamp(
            now.timestamp() + cooldown_sec, tz=timezone.utc
        ).isoformat(),
        "lastSuccessAt": prev.get("lastSuccessAt"),
        "uncertain": True,
    }
    sources[key] = entry
    _write_json(_health_path(workspace), data)
    return entry


def record_source_success(workspace: Workspace, source: str) -> dict[str, Any]:
    key = _source_key(source)
    data = load_source_health(workspace)
    sources = data.setdefault("sources", {})
    prev = sources.get(key) if isinstance(sources.get(key), dict) else {}
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "source": key,
        "failures": int(prev.get("failures") or 0),
        "lastError": prev.get("lastError"),
        "lastFailureAt": prev.get("lastFailureAt"),
        "cooldownUntil": prev.get("cooldownUntil"),
        "lastSuccessAt": now,
        "uncertain": bool(prev.get("failures")),
    }
    sources[key] = entry
    _write_json(_health_path(workspace), data)
    return entry


def source_in_cooldown(workspace: Workspace, source: str) -> dict[str, Any] | None:
    key = _source_key(source)
    entry = load_source_health(workspace).get("sources", {}).get(key)
    if not isinstance(entry, dict):
        return None
    until = entry.get("cooldownUntil")
    if not until:
        return None
    try:
        deadline = datetime.fromisoformat(str(until))
    except ValueError:
        return None
    if datetime.now(timezone.utc) < deadline:
        return entry
    return None


def explain_source_path(workspace: Workspace, source: str) -> str:
    entry = load_source_health(workspace).get("sources", {}).get(_source_key(source), {})
    if not isinstance(entry, dict) or not entry:
        return f"本次构建路径：{source}（无历史失败记录）"
    cooling = source_in_cooldown(workspace, source)
    extra = "仍在冷却，失败不代表源已恢复" if cooling else "短暂成功不清除历史失败"
    return (
        f"本次构建路径：{source}；历史失败 {entry.get('failures') or 0} 次；{extra}"
    )


def extract_download_sources(text: str) -> list[str]:
    found: list[str] = []
    for match in _REGISTRY_HOST_RE.finditer(text or ""):
        host = match.group(1)
        if host not in found:
            found.append(host)
    return found


def diagnose_proxy_path(*, env: dict[str, str] | None, command: str) -> str:
    """诊断必须描述容器/构建路径实际看到的代理（DEV-148）。"""
    if env is None:
        env = os.environ
    http_proxy = env.get("HTTP_PROXY") or env.get("http_proxy") or ""
    https_proxy = env.get("HTTPS_PROXY") or env.get("https_proxy") or ""
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
    sources = extract_download_sources(command)
    if not http_proxy and not https_proxy:
        hint = "构建环境未设置 HTTP_PROXY/HTTPS_PROXY"
        if sources:
            hint += f"；目标 {', '.join(sources[:3])}"
        return hint
    parsed = urlparse(http_proxy or https_proxy)
    return (
        f"构建路径代理 {parsed.scheme or 'http'}://{parsed.hostname or parsed.path}"
        f"{':' + str(parsed.port) if parsed.port else ''}"
        f"（NO_PROXY={no_proxy or '空'}）"
        + (f"；目标 {', '.join(sources[:3])}" if sources else "")
        + "。按目标决定是否走代理，不把宿主机浏览器代理当成构建路径"
    )


def mark_build_owner(
    workspace: Workspace,
    instance_id: str,
    *,
    owner: str,
    reason: str,
    pid: int | None = None,
) -> None:
    """手动构建登记 owner，自愈生成代替换时必须看见（DEV-147）。"""
    payload = {
        "instanceId": instance_id,
        "owner": owner,
        "reason": reason,
        "pid": os.getpid() if pid is None else pid,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(_owner_path(workspace, instance_id), payload)


def clear_build_owner(workspace: Workspace, instance_id: str) -> None:
    path = _owner_path(workspace, instance_id)
    path.unlink(missing_ok=True)


def manual_build_blocks_heal(workspace: Workspace, instance_id: str) -> dict[str, Any] | None:
    path = _owner_path(workspace, instance_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("owner") or "") != "manual":
        return None
    if _owner_marker_is_stale(data):
        path.unlink(missing_ok=True)
        return None
    return data


def _owner_marker_is_stale(data: dict[str, Any]) -> bool:
    pid_raw = data.get("pid")
    try:
        pid = int(pid_raw) if pid_raw is not None else None
    except (TypeError, ValueError):
        pid = None
    if pid is not None:
        return not _pid_alive(pid)
    stamped = _parse_owner_at(data.get("at"))
    if stamped is None:
        return True
    age = (datetime.now(timezone.utc) - stamped).total_seconds()
    return age > _OWNER_STALE_SEC


def _parse_owner_at(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - 正式测试在 POSIX
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            process_query_limited_information, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _owner_path(workspace: Workspace, instance_id: str) -> Path:
    folder = workspace.run / "build-owners"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{instance_id}.json"


def _source_key(source: str) -> str:
    raw = (source or "").strip() or "unknown"
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or raw).lower()
    return host


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


__all__ = [
    "clear_build_owner",
    "diagnose_proxy_path",
    "explain_source_path",
    "extract_download_sources",
    "host_build_timeout",
    "inject_pkg_cache_env",
    "load_source_health",
    "manual_build_blocks_heal",
    "mark_build_owner",
    "pkg_cache_dir",
    "record_source_failure",
    "record_source_success",
    "source_in_cooldown",
    "timeout_hint",
    "write_build_segment",
]
