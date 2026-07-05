"""Docker / Docker Compose 运行时封装（WBS-14）。

把 ``docker compose`` 子命令封装成可测试、可观测的 Python 接口：

* :meth:`DockerRuntime.build` / :meth:`up` / :meth:`stop` / :meth:`start` /
  :meth:`restart` / :meth:`down` / :meth:`logs` 对应 Compose 子命令；
* :meth:`container_id` / :meth:`image_id` / :meth:`status` 提供容器观测；
* :func:`is_available` / :func:`ensure_available` 检查 Docker 前置条件。

设计要点（对应 V1 设计说明第 13、14 节与 WBS-14）：

1. **stop 不删容器**（WBS-14.07/验收#2）：``stop`` 用 ``docker compose stop``，
   ``down`` 作为内部能力单独提供，不作为停止默认。
2. **stdout/stderr 落实例日志**（WBS-14.13）：build → ``logs/build.log``，
   up/start/restop → ``logs/run.log``，统一通过模块级 :func:`_execute` 追加写入。
3. **超时与失败**（WBS-14.12）：超时抛 :class:`DockerError`，非零退出抛
   :class:`DockerError` 并带 stderr 摘要。
4. **builds/events 落表**（WBS-14.14/15）：构造时传入 :class:`Registry` 即自动
   记录状态变化事件；``build()`` 传入 ``build_id`` 时按结果 finish 该 build 行。
   registry 为空时这些写入静默跳过，便于纯执行场景与单元测试。
5. **不依赖真实 Docker 做单元测试**：所有命令走模块级 :func:`_execute`，测试用
   monkeypatch 替换即可；真实 Docker 集成测试在 ``tests/test_docker_integration.py``
   用 skipif 守卫（WBS-14 交付物#4）。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from local_web_access.errors import DockerError
from local_web_access.logging import get_logger
from local_web_access.paths import Workspace
from local_web_access.registry import Registry

log = get_logger("docker.runtime")

_BUILD_TIMEOUT = 1800  # 镜像构建最久 30 分钟（小主机性能弱，留足余量）
_UP_TIMEOUT = 180
_STOP_TIMEOUT = 120
_QUERY_TIMEOUT = 60

# 实例日志文件名约定（与 hosting.py 保持一致）
_BUILD_LOG = "build.log"
_RUN_LOG = "run.log"


# ---- 结果数据类 --------------------------------------------------------------


@dataclass
class ComposeResult:
    """单次 Compose 子命令的执行结果。"""

    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class ContainerStatus:
    """容器观测快照（WBS-14.11）。"""

    service: str = ""
    name: str = ""
    container_id: str | None = None
    image: str = ""
    state: str = ""  # running / exited / restarting / paused / created ...
    status_text: str = ""  # "Up 5 minutes" / "Exited (0) ..."
    health: str | None = None
    ports: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def is_running(self) -> bool:
        return self.state == "running"


# ---- 模块级执行器 ------------------------------------------------------------


def _execute(
    args: list[str],
    *,
    cwd: Path,
    log_path: Path | None = None,
    timeout: int = _QUERY_TIMEOUT,
) -> ComposeResult:
    """执行外部命令（WBS-14.12 超时/失败处理、WBS-14.13 日志）。

    Args:
        args: 命令参数列表（不走 shell，避免注入）。
        cwd: 工作目录。
        log_path: 若提供，把命令与 stdout/stderr 追加写入该文件。
        timeout: 超时秒数。

    Returns:
        :class:`ComposeResult`。

    Raises:
        DockerError: 命令未找到、超时。
    """
    try:
        cp = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise DockerError(
            "docker 命令未找到：请确认 Docker 已安装且 docker 在 PATH 中",
            command=list(args),
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerError(
            f"命令超时（{timeout}s）：{' '.join(args)}",
            command=list(args),
            timeout=timeout,
        ) from exc

    out = cp.stdout or ""
    err = cp.stderr or ""
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n$ {' '.join(args)}\n")
            if out:
                fh.write(out)
            if err:
                fh.write(err)
    return ComposeResult(args=list(args), returncode=cp.returncode, stdout=out, stderr=err)


def _require_ok(result: ComposeResult, *, action: str, instance_id: str) -> ComposeResult:
    """非零退出统一转 :class:`DockerError`，带 stderr 摘要。"""
    if result.ok:
        return result
    tail = (result.stderr or result.stdout).strip().splitlines()
    summary = "\n".join(tail[-10:]) if tail else f"exit {result.returncode}"
    raise DockerError(
        f"Docker {action} 失败（实例 {instance_id}，exit {result.returncode}）：{summary}",
        instance_id=instance_id,
        action=action,
        returncode=result.returncode,
        stderr=result.stderr,
    )


# ---- DockerRuntime ----------------------------------------------------------


class DockerRuntime:
    """封装单个工作区下所有实例的 Docker Compose 操作。

    Args:
        workspace: 工作区。
        registry: 可选 registry；提供后会自动记录状态变化事件与构建结果。
    """

    def __init__(self, workspace: Workspace, registry: Registry | None = None) -> None:
        self.workspace = workspace
        self.registry = registry

    # ---- 前置条件（WBS-14.01）---------------------------------------------

    @staticmethod
    def is_available() -> bool:
        """检查 docker 与 compose 插件是否可用（不抛异常）。"""
        try:
            r = _execute(
                ["docker", "compose", "version", "--short"],
                cwd=Path.cwd(),
                timeout=10,
            )
        except DockerError:
            return False
        return r.ok

    @classmethod
    def ensure_available(cls) -> None:
        """检查 Docker 可用性，不可用时抛 :class:`DockerError`（前置条件错误）。"""
        if not cls.is_available():
            raise DockerError(
                "Docker 不可用：请确认 Docker 已安装、dockerd 正在运行、且 docker compose 子命令可用",
            )

    # ---- 构建与生命周期（WBS-14.02~07）-----------------------------------

    def build(
        self,
        instance_id: str,
        *,
        build_id: int | None = None,
        log_path: Path | None = None,
        timeout: int = _BUILD_TIMEOUT,
    ) -> ComposeResult:
        """``docker compose build``（WBS-14.02 / .14 / .13）。

        Args:
            instance_id: 实例 id。
            build_id: 若提供且 registry 已设置，按结果 finish 该 build 行。
            log_path: 默认 ``logs/build.log``。
            timeout: 构建超时秒数。
        """
        log_path = log_path or self.workspace.app_logs(instance_id) / _BUILD_LOG
        result = _execute(
            self._compose_cmd(instance_id, "build"),
            cwd=self.workspace.app_dir(instance_id),
            log_path=log_path,
            timeout=timeout,
        )
        if build_id is not None and self.registry is not None:
            if result.ok:
                self.registry.finish_build(build_id, status="success")
            else:
                self.registry.finish_build(
                    build_id,
                    status="failed",
                    error_summary=_tail(result.stderr or result.stdout, 500),
                )
        if not result.ok:
            self._event(instance_id, "error", f"镜像构建失败（exit {result.returncode}）")
            raise _require_ok(result, action="build", instance_id=instance_id)
        return result

    def up(
        self,
        instance_id: str,
        *,
        detached: bool = True,
        log_path: Path | None = None,
        timeout: int = _UP_TIMEOUT,
    ) -> ComposeResult:
        """``docker compose up``（WBS-14.03）。默认 ``-d`` 后台启动。"""
        args = self._compose_cmd(instance_id, "up")
        if detached:
            args.append("-d")
        result = _execute(
            args,
            cwd=self.workspace.app_dir(instance_id),
            log_path=log_path or self.workspace.app_logs(instance_id) / _RUN_LOG,
            timeout=timeout,
        )
        result = _require_ok(result, action="up", instance_id=instance_id)
        self._event(instance_id, "start", "容器已通过 docker compose up 启动")
        return result

    def stop(
        self,
        instance_id: str,
        *,
        log_path: Path | None = None,
        timeout: int = _STOP_TIMEOUT,
    ) -> ComposeResult:
        """``docker compose stop``（WBS-14.04）——停止但不删除容器。"""
        result = _execute(
            self._compose_cmd(instance_id, "stop"),
            cwd=self.workspace.app_dir(instance_id),
            log_path=log_path or self.workspace.app_logs(instance_id) / _RUN_LOG,
            timeout=timeout,
        )
        result = _require_ok(result, action="stop", instance_id=instance_id)
        self._event(instance_id, "stop", "容器已停止（compose stop，容器与卷保留）")
        return result

    def start(
        self,
        instance_id: str,
        *,
        log_path: Path | None = None,
        timeout: int = _UP_TIMEOUT,
    ) -> ComposeResult:
        """``docker compose start``（WBS-14.05）——从 stopped 状态恢复。"""
        result = _execute(
            self._compose_cmd(instance_id, "start"),
            cwd=self.workspace.app_dir(instance_id),
            log_path=log_path or self.workspace.app_logs(instance_id) / _RUN_LOG,
            timeout=timeout,
        )
        result = _require_ok(result, action="start", instance_id=instance_id)
        self._event(instance_id, "start", "容器已从 stopped 状态恢复运行")
        return result

    def restart(
        self,
        instance_id: str,
        *,
        log_path: Path | None = None,
        timeout: int = _UP_TIMEOUT,
    ) -> ComposeResult:
        """``docker compose restart``（WBS-14.06）。"""
        result = _execute(
            self._compose_cmd(instance_id, "restart"),
            cwd=self.workspace.app_dir(instance_id),
            log_path=log_path or self.workspace.app_logs(instance_id) / _RUN_LOG,
            timeout=timeout,
        )
        result = _require_ok(result, action="restart", instance_id=instance_id)
        self._event(instance_id, "restart", "容器已重启")
        return result

    def down(
        self,
        instance_id: str,
        *,
        remove_volumes: bool = False,
        log_path: Path | None = None,
        timeout: int = _STOP_TIMEOUT,
    ) -> ComposeResult:
        """``docker compose down``（WBS-14.07）——内部能力，**不作为 stop 默认**。

        会删除容器与网络；``remove_volumes=True`` 同时删命名卷。
        实例的 bind mount（``data/``）不受影响。
        """
        args = self._compose_cmd(instance_id, "down")
        if remove_volumes:
            args.append("-v")
        result = _execute(
            args,
            cwd=self.workspace.app_dir(instance_id),
            log_path=log_path or self.workspace.app_logs(instance_id) / _RUN_LOG,
            timeout=timeout,
        )
        result = _require_ok(result, action="down", instance_id=instance_id)
        self._event(instance_id, "down", "容器已 down（容器与网络已清理）")
        return result

    # ---- 日志（WBS-14.08）-------------------------------------------------

    def logs(
        self,
        instance_id: str,
        *,
        tail: int = 200,
        since: str | None = None,
    ) -> str:
        """``docker compose logs``，返回最近 ``tail`` 行（WBS-14.08）。"""
        args = self._compose_cmd(instance_id, "logs", "--no-color", "--tail", str(tail))
        if since:
            args += ["--since", since]
        result = _execute(
            args,
            cwd=self.workspace.app_dir(instance_id),
            timeout=_QUERY_TIMEOUT,
        )
        # logs 即便容器已退出也可能返回 0；非零才报错
        if not result.ok:
            raise DockerError(
                f"获取日志失败（实例 {instance_id}，exit {result.returncode}）：{result.stderr.strip()}",
                instance_id=instance_id,
            )
        return result.stdout

    # ---- 观测（WBS-14.09~11）---------------------------------------------

    def container_id(self, instance_id: str) -> str | None:
        """查询 service 容器 id（WBS-14.09）。

        使用 ``docker compose ps -q``，返回短 id；无运行容器时返回 None。
        """
        result = _execute(
            self._compose_cmd(instance_id, "ps", "-q"),
            cwd=self.workspace.app_dir(instance_id),
            timeout=_QUERY_TIMEOUT,
        )
        if not result.ok:
            return None
        cid = result.stdout.strip().splitlines()
        return cid[0] if cid else None

    def image_id(self, instance_id: str) -> str | None:
        """查询镜像 id（WBS-14.10）。

        优先用容器 inspect 取 ``.Image``；容器不存在时回退到
        ``docker images -q <project>-<service>``（Compose 默认镜像命名）。
        """
        cid = self.container_id(instance_id)
        if cid:
            r = _execute(
                ["docker", "inspect", cid, "--format", "{{.Image}}"],
                cwd=self.workspace.app_dir(instance_id),
                timeout=_QUERY_TIMEOUT,
            )
            if r.ok and r.stdout.strip():
                return r.stdout.strip()

        project, service = self._project_service(instance_id)
        r = _execute(
            ["docker", "images", "-q", f"{project}-{service}"],
            cwd=self.workspace.app_dir(instance_id),
            timeout=_QUERY_TIMEOUT,
        )
        if r.ok:
            lines = r.stdout.strip().splitlines()
            return lines[0] if lines else None
        return None

    def status(self, instance_id: str) -> ContainerStatus | None:
        """容器状态观测（WBS-14.11）。无容器时返回 None。"""
        result = _execute(
            self._compose_cmd(instance_id, "ps", "--format", "json", "--all"),
            cwd=self.workspace.app_dir(instance_id),
            timeout=_QUERY_TIMEOUT,
        )
        if not result.ok:
            return None
        for data in _iter_ps_json(result.stdout):
            return ContainerStatus(
                service=data.get("Service", ""),
                name=data.get("Name", ""),
                container_id=data.get("Id") or data.get("ContainerID"),
                image=data.get("Image", ""),
                state=(data.get("State") or "").lower(),
                status_text=data.get("Status", ""),
                health=data.get("Health"),
                ports=_extract_ports(data),
                raw=data,
            )
        return None

    def is_running(self, instance_id: str) -> bool:
        """容器是否处于 running 状态。"""
        st = self.status(instance_id)
        return st is not None and st.is_running

    # ---- 内部辅助 ----------------------------------------------------------

    def _compose_cmd(self, instance_id: str, *args: str) -> list[str]:
        """组装 ``docker compose --env-file ... -f compose.yaml <sub>``。"""
        compose_file = self.workspace.app_compose_path(instance_id)
        env_file = self.workspace.app_env_path(instance_id)
        cmd: list[str] = ["docker", "compose"]
        if env_file.is_file():
            cmd += ["--env-file", str(env_file)]
        cmd += ["-f", str(compose_file)]
        cmd += list(args)
        return cmd

    def _project_service(self, instance_id: str) -> tuple[str, str]:
        """从 registry / manifest 兜底取 (projectName, serviceName)。"""
        project = f"lwa-{instance_id}"
        service = "app"
        if self.registry is not None:
            row = self.registry.get_container(instance_id)
            if row:
                project = row.get("compose_project") or project
                service = row.get("service_name") or service
        return project, service

    def _event(self, instance_id: str, event_type: str, message: str) -> None:
        """记录状态变化事件（WBS-14.15）。registry 未设置时跳过。"""
        if self.registry is None:
            return
        try:
            self.registry.add_event(instance_id, event_type, message)
        except Exception:  # noqa: BLE001
            log.exception("写入事件失败")


# ---- 模块级便捷函数 ----------------------------------------------------------


def is_available() -> bool:
    """模块级快捷：检查 Docker 可用性（WBS-14.01）。"""
    return DockerRuntime.is_available()


def ensure_available() -> None:
    """模块级快捷：Docker 不可用时抛 :class:`DockerError`。"""
    DockerRuntime.ensure_available()


# ---- 解析辅助 ----------------------------------------------------------------


def _iter_ps_json(stdout: str):
    """解析 ``docker compose ps --format json`` 输出。

    Compose v2 按行输出 JSON 对象；个别版本输出单个 JSON 数组。两种都兼容。
    """
    text = stdout.strip()
    if not text:
        return
    # 先尝试整体当作 JSON 数组
    try:
        data = json.loads(text)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    yield item
            return
        if isinstance(data, dict):
            yield data
            return
    except json.JSONDecodeError:
        pass
    # 退回到逐行解析
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            yield data


def _extract_ports(data: dict) -> list[str]:
    """从 ps JSON 提取端口映射列表。"""
    pubs = data.get("Publishers")
    if isinstance(pubs, list):
        out = []
        for p in pubs:
            if not isinstance(p, dict):
                continue
            pub = p.get("PublishedPort")
            tgt = p.get("TargetPort")
            url = p.get("URL")
            if url:
                out.append(str(url))
            elif pub and tgt:
                out.append(f"{pub}->{tgt}")
        return out
    # 旧格式：Ports 字段为字符串
    ports = data.get("Ports")
    if isinstance(ports, str) and ports:
        return [ports]
    return []


def _tail(text: str, n: int) -> str:
    """取文本末尾 n 个字符（用于 error_summary 截断）。"""
    return text[-n:] if text else ""


__all__ = [
    "DockerRuntime",
    "ComposeResult",
    "ContainerStatus",
    "is_available",
    "ensure_available",
]
