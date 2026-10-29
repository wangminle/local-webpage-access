"""工作区构建代理：配置落盘与宿主机/构建容器两侧的环境。

代理只进入构建。镜像构建参数走 compose ``build.args`` 与 Docker 内置代理
构建参数（``HTTP_PROXY`` 等），它们默认排除在镜像历史与构建缓存之外；
不要在 Dockerfile 里显式 ``ARG HTTP_PROXY``——显式声明会让地址和凭据
进入 ``docker history``（BUG-969）。运行中容器不继承这个地址。

宿主机回环代理（127.0.0.1 等）在构建容器内不可达，容器视角地址改写为
``host.docker.internal``（BUG-968，见 :meth:`BuildProxy.container_url`）。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import yaml

from local_webpage_access import file_lock
from local_webpage_access.config import BuildProxy, Config
from local_webpage_access.errors import ConfigError
from local_webpage_access.paths import Workspace

# BUG-970：按 YAML 节点边界定位 buildProxy 映射，而非正则片段——块内空行、
# 缩进注释同属该节点；块止于下一个顶层的非空非注释行。
_BLOCK_START_RE = re.compile(r"^buildProxy:(?:\s|$)")


def detect_system_proxy() -> str:
    """读取管理服务宿主机的代理设置，返回构建支持的首个地址。"""
    import urllib.request

    from pydantic import ValidationError

    proxies = urllib.request.getproxies()
    for key in ("http", "https", "all"):
        url = (proxies.get(key) or "").strip()
        if not url:
            continue
        if "://" not in url:
            url = "http://" + url
        try:
            return BuildProxy(url=url).url
        except (ValidationError, ValueError):
            continue
    return ""


def subprocess_env(config: Config) -> dict[str, str] | None:
    """``docker compose build`` 用的环境。未启用时返回 None，表示沿用父进程环境。"""
    extra = config.buildProxy.proxy_env()
    if not extra:
        return None
    env = os.environ.copy()
    env.update(extra)
    return env


_PROXY_CASE_PAIRS = (
    ("HTTP_PROXY", "http_proxy"),
    ("HTTPS_PROXY", "https_proxy"),
    ("NO_PROXY", "no_proxy"),
)


def overlay_build_env(base: dict[str, str], instance_env: dict[str, str]) -> dict[str, str]:
    """把实例 buildEnv 叠到工作区代理上，并让同一变量的大小写保持一致。

    pip 先读小写 ``http_proxy``。实例只写 ``HTTP_PROXY`` 时，小写若仍是工作区
    地址，实例设置会被忽略。某一侧被实例显式设置后，另一侧改成同一个值。
    """
    merged = {**base, **instance_env}
    for upper, lower in _PROXY_CASE_PAIRS:
        upper_set = upper in instance_env
        lower_set = lower in instance_env
        if upper_set and not lower_set:
            merged[lower] = instance_env[upper]
        elif lower_set and not upper_set:
            merged[upper] = instance_env[lower]
    return merged


def config_with_latest_build_proxy(workspace: Workspace, config: Config) -> Config:
    """构建入口使用的配置：只把 ``buildProxy`` 换成磁盘上的最新值。

    daemon 与其它长驻进程在启动时加载 Config。管理页保存代理只更新管理进程
    自己的内存，自动部署若继续用启动时的对象，下一次构建看不到新地址。
    """
    from local_webpage_access.config import load_config

    latest = load_config(workspace).buildProxy
    current = config.buildProxy
    if current.enabled == latest.enabled and current.url == latest.url:
        return config
    return config.model_copy(update={"buildProxy": latest})


def merge_into(base: dict[str, str], config: Config) -> dict[str, str]:
    """宿主机前端安装/构建环境。实例自己的 buildEnv 应在此之后经 overlay_build_env 合并。"""
    extra = config.buildProxy.proxy_env()
    if not extra:
        return base
    return {**base, **extra}


def render_block(proxy: BuildProxy) -> str:
    enabled = "true" if proxy.enabled else "false"
    url = json.dumps(proxy.url, ensure_ascii=False)
    return f"buildProxy:\n  enabled: {enabled}\n  url: {url}\n"


def _replace_block(text: str, block: str) -> str:
    """用 ``block`` 替换文本中的顶层 ``buildProxy`` 节点；不存在则追加到末尾。

    节点范围 = ``buildProxy:`` 行及其后所有缩进行，含夹在中间的空行/注释行
    （BUG-970：旧正则在空行停止，遗留的旧 ``url`` 会覆盖新值）。空行/注释行
    之后的首个内容行不再缩进时，这些行归后续内容，予以保留。
    """
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if _BLOCK_START_RE.match(line)), None
    )
    if start is None:
        prefix = text.rstrip("\n")
        return (prefix + "\n\n" if prefix else "") + block
    end = len(lines)
    j = start + 1
    while j < len(lines):
        stripped = lines[j].strip()
        if not stripped or stripped.startswith("#"):
            k = j + 1
            while k < len(lines) and (
                not lines[k].strip() or lines[k].strip().startswith("#")
            ):
                k += 1
            if k < len(lines) and lines[k][0] in (" ", "\t"):
                j = k
                continue
            end = j
            break
        if lines[j][0] in (" ", "\t"):
            j += 1
            continue
        end = j
        break
    return "".join(lines[:start]) + block + "".join(lines[end:])


def _validate_full_config(updated_text: str, checked: BuildProxy) -> None:
    """写盘前校验更新后的**完整**配置（BUG-970）。

    任一失败都抛 :class:`ConfigError`，调用方不得触碰磁盘。除了解析与模型
    校验，还要确认加载结果与本次写入值一致——文件里若残留第二个
    ``buildProxy`` 段（含旧实现的损坏产物），YAML 后值覆盖前值，必须拒绝。
    """
    try:
        raw = yaml.safe_load(updated_text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"已中止写入：更新后的配置 YAML 解析失败：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("已中止写入：更新后的配置顶层不是映射")
    # 与 load_config 的旧值兼容口径一致：nginx 视为 builtin（仅用于校验，
    # 不改写落盘内容——持久迁移由 `lwa update` 负责）。
    if raw.get("staticGateway") == "nginx":
        raw["staticGateway"] = "builtin"
    parsed = Config.from_dict(raw)
    if (
        parsed.buildProxy.enabled != checked.enabled
        or parsed.buildProxy.url != checked.url
    ):
        raise ConfigError(
            "已中止写入：配置中存在重复的 buildProxy 段，旧值会覆盖新值，"
            "请手工删除多余段落后重试"
        )


_SAVE_LOCK_TIMEOUT_S = 10.0
_SAVE_LOCK_INTERVAL_S = 0.05


@contextlib.contextmanager
def _save_lock(path: Path) -> Iterator[None]:
    """配置保存互斥：保护「读-改-校验-替换」全程（BUG-1000）。

    锁文件与配置同目录、独立 inode，持锁期间不 unlink（见
    :mod:`local_webpage_access.file_lock` 约定）。flock 按 open-file-description
    互斥，同进程线程与跨进程由同一把锁覆盖。
    """
    lock_path = path.with_name(f".{path.name}.save-lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + _SAVE_LOCK_TIMEOUT_S
        while True:
            try:
                file_lock.try_acquire_exclusive(fd)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConfigError(
                        "另一个配置保存操作进行中，请稍后重试"
                    ) from None
                time.sleep(_SAVE_LOCK_INTERVAL_S)
        file_lock.ensure_lockable(fd)
        try:
            yield
        finally:
            file_lock.release_exclusive(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, content: str) -> None:
    """临时文件 + ``os.replace``，崩溃不留半写配置。

    BUG-999：代理 URL 可能含凭据。目标权限最多保留 ``0o600`` 中的位，否则默认
    ``0o600``；权限在 ``os.open`` 创建时一次落定，不存在「先 0644 再收紧」
    的窗口。BUG-1000：临时名带 UUID，并发保存不再共用同一临时文件。
    """
    if path.is_file():
        mode = stat.S_IMODE(path.stat().st_mode) & 0o600
    else:
        mode = 0o600
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_build_proxy(path: Path, proxy: BuildProxy) -> BuildProxy:
    """校验并写回 ``buildProxy`` 段，保留文件里其余内容和注释。

    BUG-970：按 YAML 节点边界替换；写盘前先对更新后的完整配置做解析、
    模型与一致性校验，失败时磁盘保持原样（旧实现先写坏文件再让
    ``load_config`` 报错，配置从此无法加载）。
    """
    checked = BuildProxy.model_validate({"enabled": proxy.enabled, "url": proxy.url})
    block = render_block(checked)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _save_lock(path):
        if not path.is_file():
            _validate_full_config(block, checked)
            _atomic_write(path, block)
            return checked
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"读取配置失败，已中止写入：{exc}") from exc
        updated = _replace_block(text, block)
        if not updated.endswith("\n"):
            updated += "\n"
        _validate_full_config(updated, checked)
        _atomic_write(path, updated)
    return checked
