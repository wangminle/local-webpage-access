"""静态网关（WBS-09）。

V1 静态托管的两条路径：
1. **Caddy 模式**：生成 ``static-gateway/sites/<id>.conf``，组装主 Caddyfile 并 reload。
2. **builtin 模式**（兜底）：为每个启用的站点在 hostPort 上启动一个
   ``python -m http.server`` 子进程，``--directory`` 指向 ``public/``。

默认优先 Caddy；若环境中没有 caddy 可执行文件，自动降级到 builtin。
每个站点拥有独立 hostPort，``enable``/``disable`` 通过启停服务模拟开关。

对应 V1 设计说明第 6 节。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path

from local_web_access.config import Config
from local_web_access.errors import GatewayError
from local_web_access.logging import get_logger, write_instance_log
from local_web_access.paths import Workspace

log = get_logger("gateway")

_HEALTH_TIMEOUT = 5
_START_WAIT = 3.0
_KILL_TIMEOUT = 10

# builtin 模式回退用的 Caddy 配置模板（也用于 Caddy 模式渲染）
_FALLBACK_TEMPLATE = """\
# Local Web Access — Caddy 静态站点配置
# 由 lwa 自动生成，请勿手动编辑。
:{host_port} {{
\troot * {root}
\tfile_server
\tencode gzip
}}
"""


class StaticGateway:
    """静态站点网关：管理多个静态 HTTP 服务。"""

    def __init__(self, workspace: Workspace, config: Config) -> None:
        self.ws = workspace
        self.config = config

    # ---- 后端探测 -----------------------------------------------------------

    def detect_backend(self) -> str:
        """返回 ``"caddy"`` 或 ``"builtin"``，遵循 ``config.staticGateway``（BUG-003）。"""
        configured = self.config.staticGateway
        if configured == "builtin":
            return "builtin"
        if configured == "caddy":
            if shutil.which("caddy"):
                return "caddy"
            log.warning("配置 staticGateway=caddy 但未找到 caddy 可执行文件，降级 builtin")
            return "builtin"
        # nginx 等尚未实现的网关：暂降级 builtin
        log.warning("staticGateway=%s 尚未实现，降级 builtin", configured)
        return "builtin"

    # ---- 站点配置路径 -------------------------------------------------------

    def site_config_path(self, instance_id: str) -> Path:
        return self.ws.app_gateway_config(instance_id)

    def main_config_path(self) -> Path:
        return self.ws.static_gateway / "Caddyfile"

    def _load_template(self) -> str:
        """读取 Caddy 站点模板，找不到时用内置兜底。"""
        candidates = [
            self.ws.templates / "static" / "caddy_site.conf.tpl",
        ]
        for path in candidates:
            if path.is_file():
                try:
                    return path.read_text(encoding="utf-8")
                except OSError:
                    continue
        return _FALLBACK_TEMPLATE

    def generate_site_config(
        self, instance_id: str, host_port: int, root: Path
    ) -> Path:
        """渲染并写入 ``static-gateway/sites/<id>.conf``（WBS-09.03）。"""
        template = self._load_template()
        content = template.format(
            host_port=host_port,
            root=str(root).replace("\\", "/"),
            site_id=instance_id,
        )
        path = self.site_config_path(instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.info("已生成站点配置：%s", path)
        return path

    def remove_site_config(self, instance_id: str) -> None:
        path = self.site_config_path(instance_id)
        if path.exists():
            path.unlink()

    # ---- enable / disable ---------------------------------------------------

    def enable(
        self,
        instance_id: str,
        host_port: int,
        root: Path,
        *,
        wait_health: bool = True,
    ) -> None:
        """启用静态站点（WBS-09.04/05/06/07）。

        builtin 模式：启动 http.server 子进程，随后做健康检查；
        健康检查失败时回滚（停掉进程、删除配置）。
        Caddy 模式：生成站点配置并 reload，reload 失败回滚。
        """
        root = Path(root)
        if not root.is_dir():
            raise GatewayError(
                f"静态根目录不存在：{root}",
                instance_id=instance_id,
                root=str(root),
            )
        self.generate_site_config(instance_id, host_port, root)

        backend = self.detect_backend()
        if backend == "builtin":
            self._start_builtin(instance_id, host_port, root)
            if wait_health and not self.health_check(host_port):
                # 回滚
                self._stop_builtin(instance_id)
                self.remove_site_config(instance_id)
                raise GatewayError(
                    f"静态站点启动后健康检查失败（端口 {host_port}）",
                    instance_id=instance_id,
                    host_port=host_port,
                )
        else:
            # Caddy 模式：reload 主配置，失败则回滚站点配置
            try:
                self.reload_all()
            except GatewayError:
                self.remove_site_config(instance_id)
                raise
        log.info("静态站点已启用：%s（%s，端口 %d）", instance_id, backend, host_port)

    def disable(self, instance_id: str) -> None:
        """禁用静态站点（WBS-09.07）。"""
        backend = self.detect_backend()
        if backend == "builtin":
            self._stop_builtin(instance_id)
        else:
            self.remove_site_config(instance_id)
            try:
                self.reload_all()
            except GatewayError as exc:
                log.warning("禁用 %s 后 Caddy reload 失败：%s", instance_id, exc)
        # builtin 模式也清理配置
        self.remove_site_config(instance_id)
        log.info("静态站点已禁用：%s", instance_id)

    def is_enabled(self, instance_id: str) -> bool:
        """站点是否处于启用状态（PID 存在且端口在监听）。"""
        pid = self._read_pid(instance_id)
        if pid is None:
            return False
        return self._pid_alive(pid)

    # ---- 健康检查 -----------------------------------------------------------

    def health_check(self, host_port: int, *, timeout: float = _HEALTH_TIMEOUT) -> bool:
        """HTTP GET ``/`` 检查站点是否在服务（WBS-09.08）。"""
        url = f"http://127.0.0.1:{host_port}/"
        try:
            resp = urllib.request.urlopen(url, timeout=timeout)
            return 200 <= resp.status < 400
        except Exception:  # noqa: BLE001
            return False

    # ---- Caddy reload + 回滚 ------------------------------------------------

    def reload_all(self) -> None:
        """组装主 Caddyfile 并 reload（WBS-09.05/06）。

        builtin 模式下为空操作。Caddy 模式下失败会回滚到上一份主配置。
        """
        if self.detect_backend() != "caddy":
            return

        main = self.main_config_path()
        main.parent.mkdir(parents=True, exist_ok=True)
        backup = main.with_suffix(".bak")
        previous = main.read_text(encoding="utf-8") if main.exists() else None
        new_content = self._assemble_main_config()

        if previous is not None:
            backup.write_text(previous, encoding="utf-8")
        main.write_text(new_content, encoding="utf-8")

        result = subprocess.run(
            ["caddy", "reload", "--config", str(main)],
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            # 回滚
            if previous is not None:
                main.write_text(previous, encoding="utf-8")
                subprocess.run(
                    ["caddy", "reload", "--config", str(main)],
                    capture_output=True,
                    timeout=15,
                )
            else:
                # 首次生成即失败：删除坏配置，避免残留的非法 Caddyfile 影响后续 reload
                try:
                    main.unlink()
                except OSError:
                    pass
            raise GatewayError(
                "Caddy reload 失败",
                stderr=result.stderr.decode("utf-8", "replace"),
            )

    def _assemble_main_config(self) -> str:
        """汇总所有已生成的站点配置为 Caddyfile。"""
        lines = ["{", "\tadmin off", "}", ""]
        sites = sorted(self.ws.static_sites.glob("*.conf"))
        for site in sites:
            lines.append(f"import {site.as_posix()}")
        return "\n".join(lines) + "\n"

    # ---- builtin 进程管理 ---------------------------------------------------

    def _pid_path(self, instance_id: str) -> Path:
        return self.ws.run / f"static-{instance_id}.pid"

    def _write_pid(self, instance_id: str, pid: int) -> None:
        self._pid_path(instance_id).parent.mkdir(parents=True, exist_ok=True)
        self._pid_path(instance_id).write_text(str(pid), encoding="utf-8")

    def _read_pid(self, instance_id: str) -> int | None:
        path = self._pid_path(instance_id)
        if not path.is_file():
            return None
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None

    def _clear_pid(self, instance_id: str) -> None:
        path = self._pid_path(instance_id)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    def _start_builtin(self, instance_id: str, host_port: int, root: Path) -> None:
        """启动一个 ``python -m http.server`` 子进程。"""
        log_path = self.ws.app_logs(instance_id) / "gateway.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")

        cmd = [
            sys.executable,
            "-u",
            "-m",
            "http.server",
            str(host_port),
            "--directory",
            str(root),
            "--bind",
            "0.0.0.0",
        ]
        popen_kwargs: dict = {
            "stdout": log_fh,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            )
        else:
            popen_kwargs["start_new_session"] = True

        try:
            try:
                proc = subprocess.Popen(cmd, **popen_kwargs)
            except OSError as exc:
                raise GatewayError(
                    f"启动内置静态服务失败：{exc}",
                    instance_id=instance_id,
                ) from exc
        finally:
            # 子进程已通过继承拿到 stdout 句柄，父进程侧必须关闭自己的副本，
            # 否则会锁住 gateway.log（Windows 下导致实例目录/gateway.log 无法删除，BUG-006）。
            log_fh.close()
        self._write_pid(instance_id, proc.pid)
        write_instance_log(
            self.ws.apps,
            instance_id,
            "gateway",
            f"启动内置静态服务 pid={proc.pid} port={host_port} root={root}",
        )

    def _stop_builtin(self, instance_id: str) -> None:
        pid = self._read_pid(instance_id)
        if pid is None:
            return
        self._kill_process(pid)
        self._clear_pid(instance_id)
        write_instance_log(
            self.ws.apps,
            instance_id,
            "gateway",
            f"停止内置静态服务 pid={pid}",
        )

    def _kill_process(self, pid: int) -> None:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=_KILL_TIMEOUT,
                )
            else:
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    return
            log.info("已终止静态服务进程 pid=%d", pid)
        except (ProcessLookupError, OSError):
            pass

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """跨平台检查进程是否存活。"""
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                handle = kernel32.OpenProcess(0x1000, False, pid)  # SYNCHRONIZE
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            except Exception:  # noqa: BLE001
                return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False


__all__ = ["StaticGateway"]
