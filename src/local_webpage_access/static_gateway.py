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
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from local_webpage_access.config import Config
from local_webpage_access.daemon import pid_cmdline_contains
from local_webpage_access.errors import GatewayError
from local_webpage_access.logging import get_logger, write_instance_log
from local_webpage_access.paths import Workspace
from local_webpage_access.probe import mark_probe_url

log = get_logger("gateway")

_HEALTH_TIMEOUT = 5
_START_WAIT = 3.0
_KILL_TIMEOUT = 10

# 从站点片段解析 :port / root（BUG-216 失败恢复用）
_SITE_PORT_RE = re.compile(r"^:(\d+)\s*\{", re.MULTILINE)
_SITE_ROOT_RE = re.compile(r"^\s*root\s+\*\s+(.+)$", re.MULTILINE)

# ---- Caddy master 生命周期（IMP-010 / BUG-069 / BUG-070）-------------------
# admin API 固定走 IPv4 loopback（macOS 上 localhost 常解析为 ::1，而 Caddy admin
# 仅监听 IPv4，见 BUG-068）。reload/start/stop 全部显式使用 127.0.0.1。
_ADMIN_BASE = "http://127.0.0.1:2019"
_ADMIN_CONFIG_URL = f"{_ADMIN_BASE}/config/"
_ADMIN_STOP_URL = f"{_ADMIN_BASE}/stop"
_ADMIN_PROBE_TIMEOUT = 1.0
_ADMIN_STARTUP_WAIT = 5.0
# BUG-121：pytest 默认禁止触碰全局 admin :2019；Caddy 单测可设此环境变量放行。
_ENV_ALLOW_CADDY_ADMIN = "LWA_ALLOW_CADDY_ADMIN"
_CADDY_OP_TIMEOUT = 15
_CADDY_START_TIMEOUT = 20
# 仅保证 Caddy admin 在线的最小 bootstrap 配置（无任何站点；真实站点由 reload_all 注入）。
# 不含 ``admin off``（BUG-014：首次加载后关闭 admin 会让后续 reload 全部失败）；
# Caddy 默认即在 :2019 暴露 admin。仅注释行 → 等价空配置。
_MIN_CADDYFILE = (
    "# lwa bootstrap：仅保证 Caddy admin 在线，真实站点由 reload_all 注入\n"
)


def _refuse_caddy_admin_in_pytest(action: str) -> None:
    """BUG-121：pytest 下默认拒绝真实 caddy reload/start，防止覆盖生产 :2019。"""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if os.environ.get(_ENV_ALLOW_CADDY_ADMIN) == "1":
        return
    raise RuntimeError(
        f"BUG-121: pytest 禁止对生产 Caddy admin :2019 执行 {action}；"
        f"请使用 staticGateway=builtin，或设置 {_ENV_ALLOW_CADDY_ADMIN}=1"
    )


# builtin 模式回退用的 Caddy 配置模板（也用于 Caddy 模式渲染）
# {rate_limit_block} 占位符由 _rate_limit_directive 填充（IMP-005）；
# 未启用限流时为空串，留下一行空行（Caddyfile 忽略）。
_FALLBACK_TEMPLATE = """\
# Local Webpage Access — Caddy 静态站点配置
# 由 lwa 自动生成，请勿手动编辑。
:{host_port} {{
\troot * {root}
\tfile_server
\tencode gzip
{rate_limit_block}
\tlog {{
\t\toutput file {access_log} {{
\t\t\troll_size 10mb
\t\t\troll_keep 3
\t\t}}
\t\tformat json
\t}}
}}
"""


def _caddy_quote(path: str) -> str:
    """对 Caddyfile 路径做安全引用（BUG-020）。

    Caddyfile 把空白作为参数分隔符，含空格的路径（Windows 用户目录常见）
    会被拆词导致 reload 失败。用反引号（Caddyfile 原始字符串）包裹最稳妥；
    路径本身含反引号时回退到双引号 + 转义。
    """
    if "`" in path:
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return f"`{path}`"


def _read_text_optional(path: Path) -> str | None:
    """读取文本文件；不存在或读失败返回 ``None``。"""
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        return None
    return None


def _unquote_caddy_path(token: str) -> str:
    """去掉 Caddyfile 路径的反引号或双引号包裹。"""
    token = token.strip()
    if len(token) >= 2 and token[0] == "`" and token[-1] == "`":
        return token[1:-1]
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return token


def _parse_site_binding(site_conf: str) -> tuple[int | None, Path | None]:
    """从站点片段解析 ``:port`` 与 ``root *``（BUG-216 恢复 builtin 用）。"""
    port: int | None = None
    root: Path | None = None
    m_port = _SITE_PORT_RE.search(site_conf)
    if m_port:
        try:
            port = int(m_port.group(1))
        except ValueError:
            port = None
    m_root = _SITE_ROOT_RE.search(site_conf)
    if m_root:
        raw = _unquote_caddy_path(m_root.group(1))
        if raw:
            root = Path(raw)
    return port, root


class StaticGateway:
    """静态站点网关：管理多个静态 HTTP 服务。"""

    def __init__(self, workspace: Workspace, config: Config) -> None:
        self.ws = workspace
        self.config = config
        # builtin 子进程的 Popen 句柄：_kill_process 必须用它回收僵尸，
        # 否则 os.kill(pid, 0) 对已退出但未回收的子进程恒返回 True（BUG-045）。
        self._procs: dict[str, subprocess.Popen] = {}
        # IMP-005：Caddy rate_limit 模块能力探测缓存（None=未探测）。
        self._supports_rate_limit: bool | None = None

    # ---- 后端探测 -----------------------------------------------------------

    def detect_backend(self) -> str:
        """返回 ``"caddy"`` 或 ``"builtin"``，遵循 ``config.staticGateway``。

        IMP-033：仅 Full Profile **禁止**静默降级 builtin；default 档即使配置
        ``staticGateway=caddy``，缺少二进制时仍按 BUG-003 的兼容承诺降级。
        """
        from local_webpage_access.capability import load_profile_state
        from local_webpage_access.errors import GatewayError

        configured = self.config.staticGateway
        profile = getattr(self.config, "profile", None) or "default"
        if profile != "full":
            state = load_profile_state(self.ws.root)
            if state.get("profile") == "full":
                profile = "full"
        strict = profile == "full"

        if configured == "builtin":
            if profile == "full":
                raise GatewayError(
                    "Full Profile 要求 Caddy，但 staticGateway=builtin；"
                    "请改为 caddy 或执行 lwa setup --full --resume",
                )
            return "builtin"
        if configured == "caddy":
            if shutil.which("caddy"):
                return "caddy"
            if strict:
                raise GatewayError(
                    "配置 staticGateway=caddy 但未找到 caddy 可执行文件"
                    "（Full/严格模式禁止降级 builtin）",
                )
            log.warning("配置 staticGateway=caddy 但未找到 caddy 可执行文件，降级 builtin")
            return "builtin"
        # nginx 等尚未实现的网关
        if strict:
            raise GatewayError(
                f"staticGateway={configured} 尚未实现，Full/严格模式禁止降级 builtin",
            )
        log.warning("staticGateway=%s 尚未实现，降级 builtin", configured)
        return "builtin"

    # ---- IMP-005：频率限制能力探测与指令生成 ---------------------------------

    def supports_rate_limit(self) -> bool:
        """探测 Caddy 是否含 ``http.handlers.rate_limit`` 模块（IMP-005）。

        结果在 :class:`StaticGateway` 实例生命周期内缓存。非 Caddy 后端、
        ``caddy`` 不在 PATH、或探测超时/失败时返回 ``False``。
        """
        if self._supports_rate_limit is not None:
            return self._supports_rate_limit
        self._supports_rate_limit = self._probe_rate_limit_module()
        return self._supports_rate_limit

    def _probe_rate_limit_module(self) -> bool:
        """执行 ``caddy list-modules`` 查找 rate_limit handler。"""
        try:
            result = subprocess.run(
                ["caddy", "list-modules"],
                capture_output=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
        if result.returncode != 0:
            return False
        text = result.stdout.decode("utf-8", "replace")
        return "http.handlers.rate_limit" in text

    def _rate_limit_directive(self, site_id: str) -> str:
        """构造 Caddy ``rate_limit`` 指令文本（IMP-005）。

        返回适合直接插入站点块（缩进一级 = 一个制表符）的指令；未启用、
        builtin 后端、或能力不可用时返回空串。

        令牌桶语义：``events=burst``（桶容量 / 瞬时突发上限），
        ``window=burst/rps`` 秒（补充速率 = ``rps``/秒）。默认 ``rps=3, burst=6``
        → ``events 6`` / ``window 2s``，即平均 3 次/秒、瞬时最多 6 次。

        能力不可用（Caddy 未装 rate_limit 模块）时记 WARN，站点仍正常访问、
        仅限流不生效——绝不因限流配置让静态站点整体下线（IMP-005 风险约束）。
        """
        rl = self.config.staticRateLimit
        if not rl.enabled:
            return ""
        backend = self.detect_backend()
        if backend != "caddy":
            # builtin / nginx 等不支持 Caddy 指令；首次注入时提示一次
            log.info(
                "staticRateLimit 已启用，但当前静态后端为 %s，限流未生效"
                "（builtin 模式暂不支持，需在反向代理层补充）",
                backend,
            )
            return ""
        if not self.supports_rate_limit():
            log.warning(
                "staticRateLimit 已启用，但 Caddy 不含 http.handlers.rate_limit "
                "模块；站点保持可访问，限流未生效。建议安装 caddy-ratelimit 插件"
                "（如 github.com/mholt/caddy-ratelimit）后重启 Caddy。"
            )
            return ""
        rps = max(1, rl.rps)
        burst = max(1, rl.burst)
        events = burst
        window_s = burst / rps
        if window_s >= 1 and abs(window_s - round(window_s)) < 1e-9:
            window_str = f"{round(window_s)}s"
        else:
            window_str = f"{max(1, round(window_s * 1000))}ms"
        # zone 名不含连字符（Caddy 标识符惯例）
        zone = f"lwa_{site_id.replace('-', '_')}"
        # {remote_host} 是 Caddy 占位符，作为字面值插入；str.format 不会处理
        # 替换值中的花括号，因此安全。
        return (
            f"\trate_limit {{\n"
            f"\t\tzone {zone} {{\n"
            f"\t\t\tkey {{remote_host}}\n"
            f"\t\t\tevents {events}\n"
            f"\t\t\twindow {window_str}\n"
            f"\t\t}}\n"
            f"\t}}"
        )

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
            root=_caddy_quote(str(root).replace("\\", "/")),
            site_id=instance_id,
            rate_limit_block=self._rate_limit_directive(instance_id),
            access_log=_caddy_quote(
                str(self.ws.logs / "static-access.log").replace("\\", "/")
            ),
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

    # ---- IMP-006：路径别名路由片段 -----------------------------------------

    def generate_alias_config(self, instance_id: str, alias: str, host_port: int) -> Path:
        """渲染路径别名路由片段，写入 ``aliases/<id>.conf``（IMP-006）。

        片段被主 Caddyfile 的统一入口块 ``import`` 进 ``:{staticGatewayPort}`` 站点。
        ``handle_path`` 自动去前缀——upstream 收到的是去掉 ``/<alias>`` 后的路径，
        避免应用看到 ``/alias/index.html`` 找不到资源；``handle /<alias>`` 处理
        无尾斜杠访问，301 到 ``/<alias>/``。

        alias slug 已由 :func:`paths.validate_path_alias` 校验为
        ``[a-z0-9-]+``，host_port 为 int，均可安全内插 Caddyfile。

        .. note:: SPA 绝对资源路径限制（IMP-006 验收项）

            ``handle_path`` 去掉 ``/<alias>`` 前缀后转发给 upstream，因此
            **相对路径资源**（``./assets/app.js``、``assets/logo.png``）能正确
            解析为 ``/<alias>/assets/...``。但**绝对路径资源**
            （``/assets/app.js``、以 ``/`` 开头的 ``src``/``href``）会绕过别名，
            直接打到统一入口根 ``/assets/...`` → 404。

            这意味着：纯静态 HTML 站点（相对路径或无外部资源）开箱即用；
            Vue/React 等 SPA 的构建产物若使用绝对 ``base: '/'``，资源会 404。
            受影响的项目应在构建时设置 ``base: './'``（Vite）或等价的相对基址，
            或继续使用 hostPort 端口直达（资源路径不受别名前缀影响）。
            ``import_zip`` 已把别名限制为 ``shared-static`` 纯静态形态，前端
            SPA 构建形态（``build_and_host_frontend``）当前不强制注入别名。
        """
        from local_webpage_access.paths import validate_path_alias

        # 防御性校验：即使上游漏调 validate_path_alias，也拒绝注入 Caddy 指令
        validate_path_alias(alias)
        content = (
            f"# IMP-006 路径别名：/{alias}/ → 127.0.0.1:{host_port}"
            f"（实例 {instance_id}，handle_path 去前缀）\n"
            f"handle_path /{alias}/* {{\n"
            f"\treverse_proxy 127.0.0.1:{host_port}\n"
            f"}}\n"
            f"handle /{alias} {{\n"
            f"\tredir /{alias}/ permanent\n"
            f"}}\n"
        )
        path = self.ws.app_alias_config(instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.info("已生成路径别名路由：/%s/ → 127.0.0.1:%d", alias, host_port)
        return path

    def remove_alias_config(self, instance_id: str) -> None:
        """删除实例的路径别名片段（IMP-006）。不存在时为空操作。"""
        path = self.ws.app_alias_config(instance_id)
        if path.exists():
            path.unlink()
            log.info("已删除路径别名路由：%s", instance_id)

    # ---- enable / disable ---------------------------------------------------

    def enable(
        self,
        instance_id: str,
        host_port: int,
        root: Path,
        *,
        wait_health: bool = True,
        alias: str | None = None,
    ) -> None:
        """启用静态站点（WBS-09.04/05/06/07）。

        builtin 模式：启动 http.server 子进程，随后做健康检查；
        健康检查失败时回滚（恢复旧配置 / 旧进程，而非留下悬空状态）。
        Caddy 模式：生成站点配置并 reload，reload 失败同样恢复旧片段。

        ``alias`` 非 ``None`` 时（IMP-006）在 Caddy 模式下额外生成路径别名
        路由片段。builtin 模式不支持别名入口，仅记 WARN 提示用户仍只走端口。

        BUG-216：变更前备份已有站点/别名配置；若本轮停掉了存活 builtin，
        失败时按备份端口与 root 重新拉起，避免「既无旧也无新」。
        """
        root = Path(root)
        if not root.is_dir():
            raise GatewayError(
                f"静态根目录不存在：{root}",
                instance_id=instance_id,
                root=str(root),
            )

        site_path = self.site_config_path(instance_id)
        alias_path = self.ws.app_alias_config(instance_id)
        prev_site = _read_text_optional(site_path)
        prev_alias = _read_text_optional(alias_path)
        prev_port, prev_root = _parse_site_binding(prev_site) if prev_site else (None, None)

        # BUG-070：清理切换 builtin↔caddy 或上次崩溃遗留的死 pid，避免状态误判。
        self._clear_stale_static_pid(instance_id)
        # G3：启用新服务前先停掉残留存活的 builtin，避免同端口双开。
        stopped_builtin = self._stop_live_builtin_if_any(instance_id)

        backend = self.detect_backend()
        try:
            self.generate_site_config(instance_id, host_port, root)
            # IMP-006：别名片段仅在 Caddy 模式下生成；builtin 多端口模式无统一入口。
            if alias is not None and backend == "caddy":
                self.generate_alias_config(instance_id, alias, host_port)
            elif alias is not None:
                log.warning(
                    "实例 %s 配置了路径别名 %s，但当前静态后端为 %s，别名入口未启用"
                    "（builtin 模式暂不支持，仅通过端口 %d 访问）",
                    instance_id, alias, backend, host_port,
                )

            if backend == "builtin":
                self._start_builtin(instance_id, host_port, root)
                if wait_health and not self._wait_until_healthy(host_port):
                    raise GatewayError(
                        f"静态站点启动后健康检查失败（端口 {host_port}）",
                        instance_id=instance_id,
                        host_port=host_port,
                    )
            else:
                self.reload_all()
        except Exception:
            self._restore_after_enable_failure(
                instance_id,
                backend=backend,
                prev_site=prev_site,
                prev_alias=prev_alias,
                restore_builtin=stopped_builtin,
                prev_port=prev_port,
                prev_root=prev_root,
            )
            raise
        log.info("静态站点已启用：%s（%s，端口 %d）", instance_id, backend, host_port)

    def _restore_after_enable_failure(
        self,
        instance_id: str,
        *,
        backend: str,
        prev_site: str | None,
        prev_alias: str | None,
        restore_builtin: bool,
        prev_port: int | None,
        prev_root: Path | None,
    ) -> None:
        """enable 失败后恢复旧站点/别名配置，并尽量拉回旧 builtin（BUG-216）。"""
        site_path = self.site_config_path(instance_id)
        alias_path = self.ws.app_alias_config(instance_id)
        try:
            if backend == "builtin":
                # 停掉本轮可能已拉起的半成品进程
                self._stop_builtin(instance_id)

            if prev_site is not None:
                site_path.parent.mkdir(parents=True, exist_ok=True)
                site_path.write_text(prev_site, encoding="utf-8")
            else:
                self.remove_site_config(instance_id)

            if prev_alias is not None:
                alias_path.parent.mkdir(parents=True, exist_ok=True)
                alias_path.write_text(prev_alias, encoding="utf-8")
            else:
                self.remove_alias_config(instance_id)

            if backend == "caddy":
                # BUG-069：按磁盘实际文件重组，杜绝悬空 import；再 best-effort reload
                self._sync_main_config()
                try:
                    if prev_site is not None:
                        self.reload_all()
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "恢复实例 %s 旧 Caddy 配置后 reload 失败（忽略）：%s",
                        instance_id,
                        exc,
                    )
            elif restore_builtin and prev_port is not None and prev_root is not None:
                if prev_root.is_dir():
                    try:
                        self._start_builtin(instance_id, prev_port, prev_root)
                        log.info(
                            "已恢复实例 %s 旧 builtin（port=%d root=%s）",
                            instance_id,
                            prev_port,
                            prev_root,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "恢复实例 %s 旧 builtin 失败（忽略）：%s",
                            instance_id,
                            exc,
                        )
                else:
                    log.warning(
                        "实例 %s 旧 root 不存在，无法恢复 builtin：%s",
                        instance_id,
                        prev_root,
                    )
        except Exception:  # noqa: BLE001 — 恢复失败不掩盖原始 enable 异常
            log.exception("实例 %s enable 失败后的配置恢复又出错", instance_id)

    def disable(self, instance_id: str) -> None:
        """禁用静态站点（WBS-09.07）。

        IMP-006：同时清理路径别名片段。BUG-069：Caddy 模式下删片段后用
        :meth:`_sync_main_config` 按磁盘实际文件重组主 Caddyfile（而非回滚到
        可能含悬空 import 的旧版本），保证主配置永不 import 已删文件。
        BUG-070：清理可能残留的 builtin 静态服务 pid。
        """
        backend = self.detect_backend()
        if backend == "builtin":
            self._stop_builtin(instance_id)
        # 两种后端都删除站点配置与别名片段
        self.remove_site_config(instance_id)
        self.remove_alias_config(instance_id)
        if backend == "caddy":
            # BUG-069：删片段后无条件按磁盘实际文件重组主 Caddyfile。
            self._sync_main_config()
        # BUG-070：清理切换后端或崩溃遗留的死 pid
        self._clear_stale_static_pid(instance_id)
        log.info("静态站点已禁用：%s", instance_id)

    def is_enabled(self, instance_id: str) -> bool:
        """站点是否处于启用状态。

        * builtin：per-instance pid（``run/static-<id>.pid``）存活；
        * Caddy：站点配置文件 ``sites/<id>.conf`` 存在——Caddy 由 master 统一服务，
          无 per-instance 进程/pid，此前仅查 pid 致 Caddy 静态站点恒判未启用
          （BUG-078：在线改路径别名因此被跳过 reload）。
        """
        if self.detect_backend() == "caddy":
            return self.site_config_path(instance_id).is_file()
        pid = self._read_pid(instance_id)
        if pid is None:
            return False
        return self._pid_alive(pid)

    # ---- 健康检查 -----------------------------------------------------------

    def health_check(
        self, host_port: int, *, timeout: float = _HEALTH_TIMEOUT, path: str = "/"
    ) -> bool:
        """HTTP GET ``path`` 检查站点是否在服务（WBS-09.08）。

        默认探 ``/``；别名统一入口端口（:staticGatewayPort）的根路径不提供服务
        （仅 ``/<alias>/`` 有路由），探测入口时应传 ``path="/<alias>/"``（BUG-080）。
        """
        url = mark_probe_url(f"http://127.0.0.1:{host_port}{path}")
        try:
            resp = urllib.request.urlopen(url, timeout=timeout)
            return 200 <= resp.status < 400
        except Exception:  # noqa: BLE001
            return False

    def _wait_until_healthy(
        self, host_port: int, *, timeout: float = _START_WAIT
    ) -> bool:
        """启动后轮询健康检查（BUG-045）。

        ``subprocess.Popen`` 返回时 ``http.server`` 已 fork 但仍在导入 / 绑定
        端口，立即一次性探测会偶发失败并触发误回滚。此前模块级 ``_START_WAIT``
        定义后从未被使用，builtin 启动后只做了一次 ``health_check``。
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.health_check(host_port):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    # ---- Caddy reload + 回滚 ------------------------------------------------

    def reload_all(self) -> None:
        """组装主 Caddyfile 并 reload（WBS-09.05/06；BUG-069 / IMP-010 修复）。

        builtin 模式下为空操作。Caddy 模式下：

        1. :meth:`ensure_caddy_running`：admin :2019 不在线则 ``caddy start`` 拉起（IMP-010/0.3）；
        2. 组装新主配置并写盘；
        3. ``caddy reload``，失败时自愈（再 ensure + reload 一次，IMP-010/0.4）；
        4. 仍失败则回滚主 Caddyfile 到上一份并抛 :class:`GatewayError`。

        本方法仅在**新增内容**（enable）路径调用；**删除**片段后的主配置重组请用
        :meth:`_sync_main_config`——后者无条件按磁盘实际文件重写，杜绝主 Caddyfile
        import 已删文件的悬空 import（BUG-069）。
        """
        if self.detect_backend() != "caddy":
            return

        access = self.verify_workspace_caddy_access()
        if access:
            raise GatewayError(
                f"工作区 Caddy 路径不可访问（{access}），禁止 reload",
                detail=access,
            )

        # IMP-033：admin 已在线时必须确认归属本工作区，禁止误操外部/系统 Caddy。
        if self._admin_alive():
            owner = self.inspect_caddy_owner()
            if owner.get("owner") != "lwa_service_user" or not owner.get(
                "workspace_match"
            ):
                raise GatewayError(
                    "Caddy master 所有权不匹配，禁止 reload",
                    detail=(
                        f"owner={owner.get('owner')} "
                        f"user={owner.get('process_user')} "
                        f"pid={owner.get('pid')}"
                    ),
                )
        else:
            # admin 不在线：尽力拉起；失败后仍走 reload，由下方统一报「reload 失败」
            self.ensure_caddy_running()

        main = self.main_config_path()
        main.parent.mkdir(parents=True, exist_ok=True)
        previous = main.read_text(encoding="utf-8") if main.exists() else None
        new_content = self._assemble_main_config()

        if previous is not None:
            backup = main.with_suffix(".bak")
            backup.write_text(previous, encoding="utf-8")
        main.write_text(new_content, encoding="utf-8")

        ok, stderr = self._reload_with_self_heal()
        if ok:
            return
        # reload 失败：回滚主 Caddyfile
        if previous is not None:
            main.write_text(previous, encoding="utf-8")
            self._reload_once()  # 尽力把旧配置 reload 回去（忽略结果）
        else:
            # 首次生成即失败：删除坏配置，避免残留非法 Caddyfile 影响后续 reload（BUG-007）
            try:
                main.unlink()
            except OSError:
                pass
        raise GatewayError("Caddy reload 失败", stderr=stderr)

    def _reload_once(self) -> tuple[bool, str]:
        """执行一次 ``caddy reload``，返回 (是否成功, stderr 文本)。"""
        try:
            _refuse_caddy_admin_in_pytest("reload")
        except RuntimeError as exc:
            # BUG-131：reload 属于可软失败路径，交给 _sync_main_config 记 WARN；
            # caddy_start 仍直接拒绝，避免测试触碰生产 admin。
            return False, str(exc)
        cmd = [
            "caddy",
            "reload",
            "--config",
            str(self.main_config_path()),
            "--adapter",
            "caddyfile",
            # macOS 上 localhost 常解析为 ::1，而 Caddy admin 仅监听 IPv4 loopback（BUG-068）
            "--address",
            "127.0.0.1:2019",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=_CADDY_OP_TIMEOUT)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            return False, str(exc)
        return result.returncode == 0, result.stderr.decode("utf-8", "replace")

    def _reload_with_self_heal(self) -> tuple[bool, str]:
        """reload 主配置；失败时探测 admin 并 (re)start 后再 reload 一次（IMP-010/0.4）。

        master 在 reload 间隙退出会让 ``caddy reload`` 报错；此时先
        :meth:`ensure_caddy_running` 拉起 master 再 reload 一次。仍失败才放弃。
        """
        ok, stderr = self._reload_once()
        if ok:
            return True, ""
        # BUG-131：pytest 防护拒绝触碰生产 admin 时不要自愈拉起 caddy_start。
        if "BUG-121" in stderr:
            return False, stderr
        log.warning(
            "caddy reload 失败，尝试自愈（ensure_caddy_running + reload）：%s",
            stderr.strip(),
        )
        if self.ensure_caddy_running():
            ok2, stderr2 = self._reload_once()
            if ok2:
                return True, ""
            return False, stderr2
        return False, stderr

    def _sync_main_config(self) -> None:
        """按磁盘实际存在的 site/alias 片段重组主 Caddyfile 并尽力 reload（BUG-069）。

        在 :meth:`enable` 失败回滚或 :meth:`disable` 删除片段后调用。
        :meth:`_assemble_main_config` 基于磁盘真实文件生成内容，因此**无条件写回**
        （不回滚到可能含悬空 import 的旧版本），保证主 Caddyfile 永不 import 已删文件。
        reload 失败仅记 WARN：配置已正确落盘，下次 ``caddy start``/reload 会加载它。
        """
        if self.detect_backend() != "caddy":
            return
        main = self.main_config_path()
        main.parent.mkdir(parents=True, exist_ok=True)
        main.write_text(self._assemble_main_config(), encoding="utf-8")
        ok, stderr = self._reload_with_self_heal()
        if not ok:
            log.warning(
                "主 Caddyfile 已按磁盘实际文件重组，但 reload 暂未成功：%s",
                stderr.strip(),
            )

    def _assemble_main_config(self) -> str:
        """汇总所有已生成的站点配置为 Caddyfile。

        不关闭 admin API：``caddy reload`` 通过 admin 端点（默认 :2019）推送
        新配置，首次加载后关闭 admin 会让后续 enable/disable 的 reload 全部
        失败（BUG-014）。import 路径用反引号引用，避免含空格的工作区路径被
        拆词（BUG-020）。

        IMP-006：当存在路径别名片段且 ``staticGatewayPort`` 已配置时，追加
        一个统一入口站点块（``:<port>``），把所有别名片段 ``import`` 进去。
        别名片段用 Python 端 glob 展开为逐条 import，避免 Caddy 引号内 glob
        的歧义。无别名或端口关闭时不追加该块，保持端口不被占用。
        """
        lines: list[str] = []
        sites = sorted(self.ws.static_sites.glob("*.conf"))
        for site in sites:
            lines.append(f"import {_caddy_quote(site.as_posix())}")

        aliases = sorted(self.ws.static_aliases.glob("*.conf"))
        port = self.config.staticGatewayPort
        if aliases and port is not None:
            lines.append("")
            lines.append(f"# IMP-006 路径别名统一入口（端口 {port}，去前缀反向代理）")
            lines.append(f":{port} {{")
            # IMP-024：统一入口块开启 JSON access log，供浏览量统计
            # （pageviews.py）按 ``/<alias>/`` 前缀归属到实例。仅别名入口流量
            # 计入浏览量；直连 hostPort 的访问不计（hostPort 多用于本机预览）。
            access_log = self.ws.logs / "static-access.log"
            access_log.parent.mkdir(parents=True, exist_ok=True)
            lines.append("\tlog {")
            lines.append(f"\t\toutput file {_caddy_quote(access_log.as_posix())} {{")
            lines.append("\t\t\troll_size 10mb")
            lines.append("\t\t\troll_keep 3")
            lines.append("\t\t}")
            lines.append("\t\tformat json")
            lines.append("\t}")
            for alias_conf in aliases:
                lines.append(f"\timport {_caddy_quote(alias_conf.as_posix())}")
            lines.append("}")
        elif aliases and port is None:
            log.warning(
                "存在 %d 个路径别名片段，但 staticGatewayPort=None，别名入口未启用"
                "（仅 hostPort 可达）；请在 local-web.yml 设置 staticGatewayPort",
                len(aliases),
            )
        return "\n".join(lines) + "\n"

    # ---- Caddy master 生命周期（IMP-010 / BUG-070）--------------------------

    def caddy_pid_path(self) -> Path:
        """Caddy master 的 pid 文件路径（``run/caddy.pid``）。"""
        return self.ws.run / "caddy.pid"

    def _admin_alive(self, *, timeout: float = _ADMIN_PROBE_TIMEOUT) -> bool:
        """探测 Caddy admin API（127.0.0.1:2019）是否在线。"""
        try:
            urllib.request.urlopen(_ADMIN_CONFIG_URL, timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 — 探测失败即视为不在线
            return False

    def _bootstrap_config_path(self) -> Path:
        """最小 bootstrap 配置路径（admin 不依赖主 Caddyfile 时使用）。"""
        return self.ws.static_gateway / ".caddy-bootstrap"

    def caddy_start(self) -> bool:
        """启动 Caddy master（IMP-010；BUG-102 假失败兜底）。

        优先用当前主 Caddyfile（若存在且非空）；否则写入最小 bootstrap 配置
        （仅保证 admin :2019 在线），真实站点由随后的 :meth:`reload_all` 注入。

        BUG-102：``caddy start --pingback`` 在 master 已成功拉起后仍可能因 pingback
        回调超时（约 20s）而抛 :class:`TimeoutExpired` 或返回非零——这是**假失败**。
        对此类失败回退用 admin :2019 探活，并要求本工作区 ``caddy.pid`` 指向存活
        进程，避免认领 pytest/外部孤儿 admin（复盘 §10.2-C2）。

        ``FileNotFoundError``（PATH 无 caddy）**立即失败**，不做 admin 兜底。
        """
        _refuse_caddy_admin_in_pytest("start")
        main = self.main_config_path()
        if main.is_file() and main.read_text(encoding="utf-8").strip():
            config_path = main
        else:
            boot = self._bootstrap_config_path()
            boot.parent.mkdir(parents=True, exist_ok=True)
            boot.write_text(_MIN_CADDYFILE, encoding="utf-8")
            config_path = boot
        cmd = [
            "caddy",
            "start",
            "--config",
            str(config_path),
            "--adapter",
            "caddyfile",
            "--pidfile",
            str(self.caddy_pid_path()),
        ]
        pingback_failed = False
        result = None
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=_CADDY_START_TIMEOUT)
        except FileNotFoundError:
            # PATH 无 caddy：真失败，绝不能用孤儿 admin 假绿（§10.2-C2）。
            log.warning("caddy start 失败：未找到 caddy 可执行文件")
            return False
        except subprocess.TimeoutExpired as exc:
            # BUG-102：pingback 超时常见为 TimeoutExpired，master 可能已起。
            log.warning("caddy start 超时（将探测 admin + pidfile 确认）：%s", exc)
            pingback_failed = True
        except OSError as exc:
            log.warning("caddy start 执行异常：%s", exc)
            return False
        if result is not None and result.returncode != 0:
            log.warning(
                "caddy start 非零退出（将探测 admin + pidfile 确认）：%s",
                result.stderr.decode("utf-8", "replace").strip(),
            )
            pingback_failed = True
        # 轮询 admin；若曾出现 pingback 类失败，还须本工作区 pidfile 存活才算成功。
        deadline = time.monotonic() + _ADMIN_STARTUP_WAIT
        while time.monotonic() < deadline:
            if self._admin_alive(timeout=0.5):
                if not pingback_failed:
                    return True
                if self._workspace_caddy_pid_alive():
                    log.warning(
                        "caddy start 的 --pingback 未确认成功，但本工作区 Caddy "
                        "admin :2019 与 pidfile 均就绪——视为启动成功（BUG-102）"
                    )
                    return True
                # admin 在线但 pidfile 非本工作区存活进程 → 疑似孤儿，勿认领
                log.warning(
                    "caddy start 失败后 admin :2019 在线，但本工作区 caddy.pid "
                    "未指向存活进程——疑似外部/测试孤儿，不视为本网关启动成功"
                )
                return False
            time.sleep(0.2)
        if self._admin_alive(timeout=0.5) and (
            not pingback_failed or self._workspace_caddy_pid_alive()
        ):
            if pingback_failed:
                log.warning(
                    "caddy start --pingback 未确认成功但本工作区 Caddy 已就绪（BUG-102）"
                )
            return True
        return False

    def _workspace_caddy_pid_alive(self) -> bool:
        """本工作区 ``run/caddy.pid`` 是否指向仍存活的进程。"""
        path = self.caddy_pid_path()
        if not path.is_file():
            return False
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return False
        return self._pid_alive(pid)

    def caddy_stop(self) -> bool:
        """停止 Caddy master（IMP-010）。通过 admin API ``POST /stop`` 优雅关闭。

        显式走 IPv4（``127.0.0.1:2019``），规避 macOS 上 ``caddy stop`` 默认连
        ``localhost``→``::1`` 的 IPv6 问题（与 BUG-068 同源）。

        BUG-176：POST /stop 前校验 :2019 归属本工作区。admin 在线但
        ``run/caddy.pid`` 不指向存活进程时，视为外部/其他工作区 Caddy，**不**
        执行 POST /stop（仅清本工作区陈旧 pid），避免关停用户自建或其他工作区
        的 master。start 侧已有归属校验（BUG-102），此处补齐 stop 侧对称防护。
        """
        if not self._admin_alive():
            self._clear_stale_caddy_pid()
            return True
        if not self._workspace_caddy_pid_alive():
            log.warning(
                "admin :2019 在线但非本工作区 Caddy（caddy.pid 不指向存活进程），"
                "跳过 POST /stop 以免关停外部/其他工作区 Caddy（BUG-176）"
            )
            self._clear_stale_caddy_pid()
            return True
        try:
            req = urllib.request.Request(_ADMIN_STOP_URL, method="POST")
            urllib.request.urlopen(req, timeout=_CADDY_OP_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — POST /stop 响应后常立即断连，属正常
            log.debug("POST /stop 返回异常（通常正常）：%s", exc)
        deadline = time.monotonic() + _ADMIN_STARTUP_WAIT
        while time.monotonic() < deadline:
            if not self._admin_alive(timeout=0.5):
                break
            time.sleep(0.2)
        stopped = not self._admin_alive()
        if stopped:
            self._clear_stale_caddy_pid()
        else:
            log.warning("Caddy master 未在预期时间内退出")
        return stopped

    def ensure_caddy_running(self) -> bool:
        """确保 Caddy admin 在线且归属本工作区（IMP-010/0.3；IMP-033 owner 校验）。

        reload 前调用：master 缺失时 reload 必失败，故先拉起。
        admin 在线但 owner 不是本工作区 LWA Caddy 时返回 False（fail-closed）。
        """
        if self._admin_alive():
            owner = self.inspect_caddy_owner()
            if owner.get("owner") == "lwa_service_user" and owner.get("workspace_match"):
                return True
            log.warning(
                "Caddy admin :2019 在线但所有权不匹配：owner=%s euid=%s pid=%s",
                owner.get("owner"),
                owner.get("process_user"),
                owner.get("pid"),
            )
            return False
        self._clear_stale_caddy_pid()
        log.warning("Caddy admin 不在线，尝试拉起 master")
        return self.caddy_start()

    def inspect_caddy_owner(self) -> dict:
        """检查 :2019 上 Caddy master 的所有权（IMP-033 / BUG-231）。

        返回 dict：``owner`` / ``process_user`` / ``pid`` / ``workspace_match`` /
        ``admin_alive`` / ``runtime``。
        """
        import getpass

        result: dict = {
            "owner": "unknown",
            "process_user": None,
            "pid": None,
            "workspace_match": False,
            "admin_alive": self._admin_alive(),
            "runtime": "unknown",
        }
        if not result["admin_alive"]:
            return result

        pid = None
        path = self.caddy_pid_path()
        if path.is_file():
            try:
                pid = int(path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pid = None
        if pid is not None and self._pid_alive(pid):
            result["pid"] = pid
            result["workspace_match"] = True
            result["process_user"] = self._process_user_for_pid(pid)
            service_user = getattr(self.config, "serviceUser", None) or getpass.getuser()
            proc_user = result["process_user"]
            if proc_user is None:
                # 无法证明进程身份时必须 fail-closed，不能把外来 Caddy 误认成本服务。
                result["owner"] = "unknown"
            elif str(proc_user) == str(service_user):
                result["owner"] = "lwa_service_user"
            else:
                result["owner"] = "foreign_process"
            result["runtime"] = (
                "ready" if result["owner"] == "lwa_service_user" else "owner_mismatch"
            )
            return result

        # admin 在线但无本工作区 pid → 系统/外部 Caddy
        result["owner"] = "system_caddy"
        result["workspace_match"] = False
        result["runtime"] = "owner_mismatch"
        # 尝试从 ss/lsof 猜 PID（best-effort，可空）
        return result

    def verify_workspace_caddy_access(self) -> str | None:
        """以当前用户预检工作区 Caddy 相关路径读写。

        返回 None 表示 OK；否则返回 ``read_denied`` / ``write_denied`` /
        ``workspace_access_denied``。
        """
        main = self.main_config_path()
        try:
            if main.is_file():
                main.read_text(encoding="utf-8")
            sites = self.ws.static_gateway / "sites"
            if sites.is_dir():
                next(sites.iterdir(), None)
        except OSError:
            return "read_denied"
        log_path = self.ws.logs / "static-access.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write("")
        except OSError:
            return "write_denied"
        return None

    def _clear_stale_caddy_pid(self) -> None:
        """清理指向已死进程的 Caddy master pid 文件（BUG-070）。"""
        path = self.caddy_pid_path()
        if not path.is_file():
            return
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
        if pid is None or not self._pid_alive(pid):
            try:
                path.unlink()
            except OSError:
                pass

    def _clear_stale_static_pid(self, instance_id: str) -> None:
        """清理指向已死进程的 builtin 静态服务 pid 文件（BUG-070）。

        切换 builtin↔caddy 或上次崩溃后，``run/static-<id>.pid`` 可能指向已退出的
        ``http.server`` 进程；及时清理避免状态误判与人工排障干扰。
        """
        path = self._pid_path(instance_id)
        if not path.is_file():
            return
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
        if pid is None or not self._pid_alive(pid):
            try:
                path.unlink()
            except OSError:
                pass

    def _stop_live_builtin_if_any(self, instance_id: str) -> bool:
        """若该实例有仍**存活**的 builtin 静态进程，停掉它（G3 / 建议 A）。

        与 :meth:`_clear_stale_static_pid`（仅清死 pid）互补：用于 :meth:`enable`
        前置——切换后端或重启用时，先把占用 hostPort 的旧 builtin 进程停掉，
        避免 builtin + caddy 在同一 hostPort 上双开。无存活进程时返回 ``False``。
        """
        pid = self._read_pid(instance_id)
        if pid is None or not self._pid_alive(pid):
            return False
        log.info(
            "启用前停掉残留存活的 builtin 静态服务 %s（pid=%d）", instance_id, pid
        )
        self._stop_builtin(instance_id)
        return True

    def stop_all_builtin(self) -> list[str]:
        """停止**所有**仍存活的 builtin 静态服务进程（G3 / 建议 A / §2.7）。

        切换到 Caddy 后端的全局交接事务（:func:`gateway_service.start_gateway`
        在拉起 caddy master 后调用）。两条途径互补，覆盖**所有**陈旧监听来源
        （切换残留 **或** pid 文件已被清理的孤儿，复盘 §2.5/§2.7 现场 65599/65793
        即后者——PPID=1、无 pid 文件）：

        1. 扫描 ``run/static-*.pid``——正常追踪的进程；
        2. 枚举服务**本工作区** ``apps/`` 的 ``http.server`` 进程——pid 文件已丢失
           的孤儿（崩溃/异常切换遗留）。仅匹配命令行同时含 ``http.server`` 与
           本工作区 ``apps/`` 路径的进程，绝不误杀其他工作区或无关 Python。

        返回被停止的实例 ID 列表。
        """
        stopped: list[str] = []
        seen_pids: set[int] = set()
        # 途径 1：pid 文件
        for pid_path in sorted(self.ws.run.glob("static-*.pid")):
            name = pid_path.name
            iid = name[len("static-") : -len(".pid")]
            if not iid:
                continue
            pid = self._read_pid(iid)
            if pid is None or not self._pid_alive(pid):
                self._clear_stale_static_pid(iid)
                continue
            log.info("切换后端：停止残留 builtin 静态服务 %s（pid=%d）", iid, pid)
            self._stop_builtin(iid)
            stopped.append(iid)
            seen_pids.add(pid)
        # 途径 2：枚举 workspace http.server（补 pid-less 孤儿）
        for pid, iid in self._enumerate_workspace_builtin_pids():
            if pid in seen_pids:
                continue
            log.info(
                "切换后端：停止残留 builtin http.server %s（pid=%d，无 pid 文件）",
                iid, pid,
            )
            if self._kill_process(pid, expected_path=self.ws.apps):
                stopped.append(iid)
                seen_pids.add(pid)
        return stopped

    def _enumerate_workspace_builtin_pids(self) -> list[tuple[int, str]]:
        """枚举服务本工作区 ``apps/`` 的 ``http.server`` 进程 (pid, inferred_iid)。

        POSIX 用 ``pgrep -lf http.server``（**必须** ``-l``：Darwin 上 ``pgrep -af``
        只输出 PID、不含命令行，会导致本方法恒返回空——复盘 §10.2-C1）。
        Windows / 无 pgrep 时返回空列表。
        仅匹配命令行同时含 ``http.server`` 与本工作区 ``apps/`` 路径的进程。
        ``iid`` 从 ``--directory <apps/<iid>/public>`` 推断；推断失败用 ``pid-<n>``。
        """
        apps_prefix = str(self.ws.apps)
        try:
            # -l：完整命令行；-f：按完整命令行匹配 pattern。勿用 -af（macOS 无 cmdline）。
            result = subprocess.run(
                ["pgrep", "-lf", "http.server"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return []
        out: list[tuple[int, str]] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or "http.server" not in line:
                continue
            if apps_prefix not in line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            iid = self._iid_from_cmdline(parts[1], apps_prefix) or f"pid-{pid}"
            out.append((pid, iid))
        return out

    @staticmethod
    def _iid_from_cmdline(cmdline: str, apps_prefix: str) -> str | None:
        """从 ``--directory <apps/<iid>/public>`` 推断 instance_id。"""
        import re

        # 形如 ... --directory /path/apps/<iid>/public [--bind ...]
        m = re.search(
            re.escape(apps_prefix) + r"/([^/]+)/public", cmdline
        )
        return m.group(1) if m else None

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
        from local_webpage_access.logs import open_append

        log_path = self.ws.app_logs(instance_id) / "gateway.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # BUG-186：打开前按大小滚动，避免 gateway.log 无限增长
        log_fh = open_append(log_path)

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
        self._procs[instance_id] = proc
        self._write_pid(instance_id, proc.pid)
        write_instance_log(
            self.ws.apps,
            instance_id,
            "gateway",
            f"启动内置静态服务 pid={proc.pid} port={host_port} root={root}",
        )

    def _stop_builtin(self, instance_id: str) -> None:
        proc = self._procs.pop(instance_id, None)
        # BUG-125：有 Popen 句柄时信任句柄 PID，不让陈旧/被篡改的 pidfile
        # 把终止目标引向无关进程；无句柄的跨实例停止才读取 pidfile 并验身份。
        pid = proc.pid if proc is not None else self._read_pid(instance_id)
        if pid is None:
            return
        if self._kill_process(
            pid,
            proc=proc,
            expected_path=self.ws.app_public(instance_id),
        ):
            self._clear_pid(instance_id)
        else:
            # kill 失败：保留 PID 文件，便于重试或人工排查（BUG-015）。
            # 进程可能仍在占端口 / 锁 gateway.log，贸然清 PID 会让它成为无法追溯的孤儿。
            log.warning(
                "终止 %s 的静态服务 pid=%d 未成功，保留 PID 文件",
                instance_id,
                pid,
            )
        write_instance_log(
            self.ws.apps,
            instance_id,
            "gateway",
            f"停止内置静态服务 pid={pid}",
        )

    def _kill_process(
        self,
        pid: int,
        *,
        proc: subprocess.Popen | None = None,
        expected_path: Path | None = None,
    ) -> bool:
        """终止进程并等待其退出（BUG-015 / BUG-045）。

        成功（含"进程已经不在"）返回 True；超时未退出返回 False。不再仅凭
        taskkill 的返回码判断——Windows 上非零退出码可能只是"进程已退出"，
        因此以轮询结果为准。

        若调用方持有该进程的 ``Popen`` 句柄（本网关自己启动的 builtin 子进程），
        必须传入 ``proc``：``_wait_for_exit`` 会用 ``proc.poll()`` 回收僵尸，
        而 ``os.kill(pid, 0)`` 对僵尸恒返回 True，会把已退出的子进程误判为
        存活，进而误报 kill 失败并保留 PID 文件（BUG-045）。
        """
        # BUG-125：没有 Popen 句柄时 PID 可能已复用。只有命令行同时包含
        # http.server 与该实例 public（或工作区 apps 前缀）才允许 killpg。
        if proc is None and self._pid_alive(pid):
            identity_path = expected_path or self.ws.apps
            if not pid_cmdline_contains(pid, "http.server", str(identity_path)):
                log.warning(
                    "静态服务 PID %d 身份不匹配，拒绝终止并清理陈旧 pidfile",
                    pid,
                )
                return True
        try:
            if os.name == "nt":
                from local_webpage_access.platform_detect import subprocess_hidden_kwargs

                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=_KILL_TIMEOUT,
                    **subprocess_hidden_kwargs(),
                )
                if result.returncode != 0:
                    stderr = result.stderr.decode("utf-8", "replace").strip()
                    log.warning(
                        "taskkill pid=%d 返回 %d：%s",
                        pid,
                        result.returncode,
                        stderr,
                    )
                    # 非零不一定是真失败（进程可能已退出），交给存活探测判定
            else:
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, OverflowError):
                    # 进程已不存在，或 pid 超出 pid_t 范围（不可能存活）→ 视为成功
                    return True
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.warning("终止进程 pid=%d 异常：%s", pid, exc)

        if self._wait_for_exit(pid, proc=proc):
            log.info("已终止静态服务进程 pid=%d", pid)
            return True
        log.warning("进程 pid=%d 未在 %ds 内退出，可能仍在运行", pid, _KILL_TIMEOUT)
        return False

    def _wait_for_exit(
        self,
        pid: int,
        *,
        proc: subprocess.Popen | None = None,
        timeout: float = _KILL_TIMEOUT,
    ) -> bool:
        """轮询进程是否已真正退出。

        持有 ``Popen`` 句柄时用 ``proc.poll()`` 判活——它会回收僵尸并给出
        退出码；``os.kill(pid, 0)`` 对僵尸进程恒返回 True（BUG-045），不能
        用来判定本网关自己启动的 builtin 子进程是否已退出。
        """

        def _exited() -> bool:
            if proc is not None:
                return proc.poll() is not None
            return not self._pid_alive(pid)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _exited():
                return True
            time.sleep(0.1)
        return _exited()

    @staticmethod
    def _process_user_for_pid(pid: int) -> str | None:
        """跨平台读取进程有效用户名（BUG-252：macOS/Windows 无 /proc）。

        Linux 优先 ``/proc/<pid>/status`` Uid；其余 POSIX 用 ``ps -o user=``；
        Windows 用 ``Invoke-CimMethod -MethodName GetOwner``（BUG-255）。
        读不到返回 None（调用方 fail-closed）。
        """
        if pid <= 0:
            return None
        # Linux：/proc/<pid>/status Uid
        status_path = Path(f"/proc/{pid}/status")
        if sys.platform.startswith("linux") and status_path.is_file():
            try:
                for line in status_path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("Uid:"):
                        euid = int(line.split()[1])
                        try:
                            import pwd

                            return pwd.getpwuid(euid).pw_name
                        except Exception:  # noqa: BLE001
                            return str(euid)
            except (OSError, ValueError, IndexError):
                pass
        if sys.platform == "win32":
            # BUG-255：CIM 实例方法须 Invoke-CimMethod，不可直接 .GetOwner()。
            # 官方示例：Get-CimInstance ... | Invoke-CimMethod -MethodName GetOwner
            from local_webpage_access.platform_detect import subprocess_hidden_kwargs

            try:
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        (
                            f"(Get-CimInstance Win32_Process -Filter "
                            f"'ProcessId={int(pid)}' | "
                            "Invoke-CimMethod -MethodName GetOwner).User"
                        ),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                    **subprocess_hidden_kwargs(),
                )
            except (FileNotFoundError, subprocess.SubprocessError, OSError):
                return None
            if result.returncode != 0:
                return None
            user = (result.stdout or "").strip()
            return user or None
        # macOS 及其他 POSIX：ps（无 /proc）
        try:
            result = subprocess.run(
                ["ps", "-o", "user=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0:
            return None
        user = (result.stdout or "").strip()
        return user or None

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
        # POSIX：先尝试回收本进程 fork 出但未 wait 的僵尸子进程。僵尸会让
        # os.kill(pid, 0) 恒返回 True（BUG-045），导致刚被 SIGTERM 杀掉的
        # builtin 子进程被误判存活，进而误报 kill 失败、端口无法释放（BUG-045）。
        # 跨 gateway 实例（hosting/daemon 每次新建 gateway）时拿不到 Popen 句柄，
        # 但子进程的父进程仍是本进程，waitpid 可正常回收；非子进程抛 ChildProcessError。
        try:
            if os.waitpid(pid, os.WNOHANG)[0]:
                return False  # 已回收 → 确已退出
        except ChildProcessError:
            pass
        except OverflowError:
            return False  # pid 超出 pid_t 范围，不可能存活
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (OSError, OverflowError):
            # OverflowError：pid 超出 pid_t 范围，绝不可能是存活进程
            return False


__all__ = ["StaticGateway"]
