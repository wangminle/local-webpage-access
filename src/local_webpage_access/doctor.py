"""``lwa doctor`` 环境与实例诊断（WBS-26）。

提供**只读**的环境健康检查与单实例排障报告。所有外部探测（Docker / 端口 /
进程）都通过可注入的 callable 完成，便于测试。

检查项（对应 WBS-26.02~11）：

* Python 版本（WBS-26.02）
* Docker 可用性（WBS-26.03）
* Docker Compose 可用性（WBS-26.04）
* 端口池可用性（WBS-26.05）
* SQLite registry（WBS-26.06）
* 静态网关（WBS-26.07）
* 磁盘空间（WBS-26.08）
* 内存与 swap（WBS-26.09）
* 单实例健康诊断（WBS-26.10）
* 修复建议（WBS-26.11，每条 failing 检查附 suggestion）
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from local_webpage_access.config import Config
from local_webpage_access.logging import get_logger
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.version_requirements import (
    MIN_CADDY_VERSION,
    MIN_COMPOSE_VERSION,
    MIN_DOCKER_VERSION,
    MIN_FASTAPI_VERSION,
    MIN_UVICORN_VERSION,
    RECOMMENDED_COMPOSE_VERSION,
    installed_package_version,
    version_ge,
)

log = get_logger("doctor")

# ---- 结果数据结构 -----------------------------------------------------------

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

# Caddy admin API 端口（与 gateway_service.ADMIN_PORT 一致，本地常量避免循环导入）。
ADMIN_DOCTOR_PORT = 2019

_ORDER = {STATUS_OK: 0, STATUS_SKIP: 1, STATUS_WARN: 2, STATUS_FAIL: 3}


@dataclass
class CheckResult:
    """单项检查结果。"""

    name: str
    status: str  # ok / warn / fail / skip
    message: str
    detail: str | None = None
    suggestion: str | None = None

    @property
    def passed(self) -> bool:
        return self.status in (STATUS_OK, STATUS_SKIP)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.detail:
            d["detail"] = self.detail
        if self.suggestion:
            d["suggestion"] = self.suggestion
        return d


@dataclass
class DoctorReport:
    """完整诊断报告。"""

    checks: list[CheckResult] = field(default_factory=list)
    instance_checks: list[CheckResult] = field(default_factory=list)
    instance_id: str | None = None
    # IMP-040：JSON 友好的 LAN 漂移摘要
    current_lan_ip: str | None = None
    drifted_instance_ids: list[str] = field(default_factory=list)
    # IMP-038：可选 access review（doctor --access）
    access_review: Any = None

    @property
    def overall(self) -> str:
        worst = STATUS_OK
        for c in self.checks + self.instance_checks:
            if _ORDER.get(c.status, 0) > _ORDER.get(worst, 0):
                worst = c.status
        return worst

    @property
    def has_failures(self) -> bool:
        return any(c.status == STATUS_FAIL for c in self.checks + self.instance_checks)

    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks + self.instance_checks if c.status == STATUS_FAIL]


# ---- 可注入的探测 callable 类型 --------------------------------------------


#: subprocess 运行器：接受 args 列表与可选 kwargs（如 capture_output），
#: 返回 CompletedProcess（含 returncode/stdout/stderr）。
#: 与 :class:`local_webpage_access.autostart.SubprocessRunner` 结构一致，
#: 使 doctor 的 runner 可直接传给 autostart 侧 API（is_enabled / linger_enabled）。
class SubprocessRunner(Protocol):
    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess: ...


#: 端口占用探测：接受端口号，返回 True 表示已被占用。
PortChecker = Callable[[int], bool]


def _default_runner(cmd: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[str]":
    """默认 subprocess 运行器：捕获输出，不在终端回显。

    接受可选 ``**kwargs``（如 ``capture_output``）并覆盖内部缺省，
    保持与 :class:`SubprocessRunner` 协议一致（避免重复传参给 ``subprocess.run``）。
    """
    run_kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": 15,
        "check": False,
    }
    run_kwargs.update(kwargs)
    try:
        return subprocess.run(cmd, **run_kwargs)
    except FileNotFoundError as exc:
        # 命令不存在 → 返回一个非零结果，由检查项解释
        return subprocess.CompletedProcess(
            args=list(cmd), returncode=127, stdout="", stderr=str(exc)
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=list(cmd), returncode=124, stdout="", stderr="timeout"
        )
    except OSError as exc:
        # 评审-组5：可执行文件存在但不可执行/损坏（PermissionError）时，
        # 统一映射为非零返回而非炸穿 run_doctor（与 FileNotFoundError 同路径）
        return subprocess.CompletedProcess(
            args=list(cmd), returncode=127, stdout="", stderr=str(exc)
        )


def _default_port_in_use(port: int) -> bool:
    """默认端口占用探测：与分配器口径一致（BUG-364：以是否有**活跃监听者**为准）。

    历史沿革：BUG-002/029 时代与分配器同用独占 bind；BUG-364 把分配器改为
    ``is_port_listening``（connect 探测，TIME_WAIT 残留不算占用）后，doctor 仍
    用 bind 探测——刚停止的实例端口在 TIME_WAIT 窗口（约 60~240s）内被误报
    FAIL"外部占用"（评审-组5）。现改委托 ``is_port_listening`` 恢复同口径。
    """
    from local_webpage_access.ports import is_port_listening

    return is_port_listening(port)


# ---- 环境检查（WBS-26.02~09）-----------------------------------------------


def check_python_version() -> CheckResult:
    """WBS-26.02：Python 版本 ≥ 3.13。"""
    info = sys.version_info
    current = f"{info.major}.{info.minor}.{info.micro}"
    if (info.major, info.minor) >= (3, 13):
        return CheckResult("python_version", STATUS_OK, f"Python {current}（满足 ≥ 3.13）")
    return CheckResult(
        "python_version",
        STATUS_FAIL,
        f"Python {current} 不满足最低要求 ≥ 3.13",
        suggestion="安装 Python 3.13+ 后重试",
    )


def check_docker(runner: SubprocessRunner = _default_runner) -> CheckResult:
    """WBS-26.03：Docker 守护进程可用，且 server 版本 ≥ 29.0.0。"""
    result = runner(["docker", "version", "--format", "{{.Server.Version}}"])
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        blob = f"{stderr}\n{stdout}"
        from local_webpage_access.docker_runtime import (
            DOCKER_PERMISSION_HINT,
            is_docker_permission_error,
        )

        # BUG-230：区分 sock 权限与引擎未启动，避免只写笼统「不可用」
        if is_docker_permission_error(blob):
            return CheckResult(
                "docker",
                STATUS_FAIL,
                "Docker 权限不足（无法访问 docker.sock）",
                detail=(stderr or stdout)[:200] or None,
                suggestion=DOCKER_PERMISSION_HINT,
            )
        return CheckResult(
            "docker",
            STATUS_FAIL,
            "Docker 不可用",
            detail=stderr[:200] or None,
            suggestion=(
                "安装 Docker 并启动 dockerd，或确认当前用户在 docker 组中；"
                "刚 usermod -aG docker 后须 newgrp/重登，并重启 lwa manager/daemon"
            ),
        )
    version = (result.stdout or "").strip()
    if not version_ge(version, MIN_DOCKER_VERSION):
        return CheckResult(
            "docker",
            STATUS_FAIL,
            f"Docker server {version} 不满足最低要求 ≥ {MIN_DOCKER_VERSION}",
            suggestion=f"升级 Docker 至 {MIN_DOCKER_VERSION} 或更高版本",
        )
    return CheckResult(
        "docker", STATUS_OK, f"Docker 可用（server {version}，≥ {MIN_DOCKER_VERSION}）"
    )


def check_docker_compose(runner: SubprocessRunner = _default_runner) -> CheckResult:
    """WBS-26.04：Docker Compose 插件可用，并区分最低线与推荐线。"""
    result = runner(["docker", "compose", "version", "--short"])
    if result.returncode != 0:
        # 回退尝试独立 compose 二进制（v1）
        result_v1 = runner(["docker-compose", "version", "--short"])
        if result_v1.returncode == 0:
            return CheckResult(
                "docker_compose",
                STATUS_FAIL,
                f"检测到 docker-compose v1（{(result_v1.stdout or '').strip()}），不满足最低要求",
                suggestion=(f"升级到 `docker compose` 插件，版本需 ≥ {MIN_COMPOSE_VERSION}"),
            )
        return CheckResult(
            "docker_compose",
            STATUS_FAIL,
            "Docker Compose 不可用",
            suggestion="安装 Docker Compose 插件（`docker compose`）",
        )
    compose_version = (result.stdout or "").strip()
    if not version_ge(compose_version, MIN_COMPOSE_VERSION):
        return CheckResult(
            "docker_compose",
            STATUS_FAIL,
            f"Docker Compose {compose_version} 不满足最低要求 ≥ {MIN_COMPOSE_VERSION}",
            suggestion=f"升级 Docker Compose 至 {MIN_COMPOSE_VERSION} 或更高版本",
        )
    if not version_ge(compose_version, RECOMMENDED_COMPOSE_VERSION):
        return CheckResult(
            "docker_compose",
            STATUS_WARN,
            f"Docker Compose {compose_version} 可用，但低于推荐版本 ≥ {RECOMMENDED_COMPOSE_VERSION}",
            suggestion=f"建议升级 Docker Compose 至 {RECOMMENDED_COMPOSE_VERSION} 或更高版本",
        )
    return CheckResult(
        "docker_compose",
        STATUS_OK,
        f"Docker Compose 可用（{compose_version}，≥ {RECOMMENDED_COMPOSE_VERSION}）",
    )


def _join_dockerfile_continuations(text: str) -> str:
    """合并以 ``\\`` 续行的 Dockerfile 逻辑行。"""
    out: list[str] = []
    acc = ""
    for line in text.splitlines():
        piece = line.rstrip()
        if acc:
            piece = acc + " " + piece.lstrip()
            acc = ""
        if piece.endswith("\\") and not piece.lstrip().startswith("#"):
            acc = piece[:-1].rstrip()
            continue
        out.append(piece)
    if acc:
        out.append(acc)
    return "\n".join(out)


def _dockerfile_from_images(text: str) -> list[str]:
    """提取 Dockerfile 外部基础镜像（跳过 ``--platform``、scratch、stage 别名）。

    CHK-261：续行 ``FROM --platform=... \\`` 不得把 ``\\`` 当镜像名；
    ``FROM scratch`` 与 ``FROM <前序 AS 别名>`` 不是需要拉取的外部镜像，
    否则 ``docker image inspect`` 必然失败，对本可离线构建的合法文件误报 WARN。
    """
    images: list[str] = []
    stages: set[str] = set()
    for line in _join_dockerfile_continuations(text).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if tokens[0].upper() != "FROM":
            continue
        rest = [tok for tok in tokens[1:] if not tok.startswith("--")]
        if not rest:
            continue
        image = rest[0]
        alias: str | None = None
        for idx, tok in enumerate(rest):
            if tok.upper() == "AS" and idx + 1 < len(rest):
                alias = rest[idx + 1]
                break
        is_internal = image.lower() == "scratch" or image in stages
        if alias:
            stages.add(alias)
        if is_internal:
            continue
        images.append(image)
    return images


def _image_registry(image: str) -> str:
    """判定基础镜像所属 registry；Docker Hub 统一归一化为 ``docker.io``。

    按 Docker 镜像名规则：第一个 ``/`` 前的分量若含 ``.``、``:`` 或为
    ``localhost``，即为 registry 主机名（ghcr.io / quay.io / …）；否则是
    Docker Hub 的 library 命名空间（``python:3.13-slim``、``docker.io/...``）。
    """
    name = image.split("@", 1)[0]
    parts = name.split("/")
    if len(parts) > 1 and (
        "." in parts[0] or ":" in parts[0] or parts[0] == "localhost"
    ):
        return parts[0]
    return "docker.io"


def _safe_json_list(raw: str) -> list[str]:
    """把 ``docker info`` 的 JSON 字段安全解析成字符串列表。

    BUG-596：Go 空切片经 JSON 序列化可能是 ``null``（不是 ``[]``），
    ``json.loads`` 返回 ``None``，直接迭代会 TypeError；此处把 null/空/
    坏值统一回退为空列表，保证 ``lwa doctor`` 不中断。
    """
    try:
        value = json.loads(raw or "[]")
    except (ValueError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [m for m in value if isinstance(m, str) and m]


def check_base_image_readiness(
    ws: Workspace, runner: SubprocessRunner = _default_runner
) -> CheckResult:
    """issue #14：基础镜像就绪度（纯本地，不做网络探测）。

    依次判定：

    1. Docker 不可用 → SKIP（check_docker 已 FAIL，不重复报）；
    2. 无容器 Dockerfile（纯静态站工作区）→ SKIP；
    3. 解析各实例 Dockerfile 的 FROM 并去重，逐个 ``docker image inspect``
       查本地缓存；
    4. 全部已缓存 → OK（构建可离线进行）；
    5. 有未缓存镜像时读 daemon 配置（``docker info`` 的 registry mirrors 与
       HTTP/HTTPS proxy）：已配 mirror/代理 → OK（拉取走加速通道，本地无法
       证实可达性，不假红）；未配任何加速 → WARN，给出平台化指引。

    刻意不做 pull / manifest inspect 在线探测——doctor 会因此变慢、受瞬时
    网络与限流影响，还可能在加速器抖动时产出假红。
    """
    probe = runner(["docker", "version", "--format", "{{.Server.Version}}"])
    if probe.returncode != 0:
        return CheckResult(
            "base_image_readiness",
            STATUS_SKIP,
            "Docker 不可用，跳过基础镜像就绪度检查（docker 检查项已报告原因）",
        )

    images: dict[str, list[str]] = {}  # image -> instance ids
    found_dockerfile = False
    for dockerfile in sorted(ws.apps.glob("*/docker/Dockerfile")):
        found_dockerfile = True
        try:
            text = dockerfile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        iid = dockerfile.relative_to(ws.apps).parts[0]
        for image in _dockerfile_from_images(text):
            images.setdefault(image, []).append(iid)

    if not images:
        message = (
            "基础镜像均为 scratch / 内部阶段别名，无外部拉取风险"
            if found_dockerfile
            else "无容器实例 Dockerfile（纯静态站），无基础镜像拉取风险"
        )
        return CheckResult("base_image_readiness", STATUS_SKIP, message)

    uncached: list[str] = []
    for image in images:
        res = runner(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
        if res.returncode != 0:
            uncached.append(image)

    cached = sorted(set(images) - set(uncached))
    if not uncached:
        return CheckResult(
            "base_image_readiness",
            STATUS_OK,
            f"基础镜像全部已在本地缓存（{', '.join(sorted(images))}），构建可离线进行",
        )

    # 有未缓存镜像：读 daemon 的 mirrors / proxy 配置（一条命令合并取）。
    info = runner(
        [
            "docker",
            "info",
            "--format",
            "{{json .RegistryConfig.Mirrors}}|{{json .HTTPProxy}}|{{json .HTTPSProxy}}",
        ]
    )
    if info.returncode != 0:
        return CheckResult(
            "base_image_readiness",
            STATUS_WARN,
            f"{len(uncached)} 个基础镜像未缓存，且未能读取 daemon 加速配置",
            detail="未缓存：" + ", ".join(uncached),
            suggestion=(
                "确认 `docker info` 可用后重跑；并配置 registry-mirrors 或代理"
                "（Linux/WSL：/etc/docker/daemon.json；Docker Desktop：Settings→Docker Engine）"
            ),
        )
    try:
        mirrors_raw, http_proxy_raw, https_proxy_raw = (
            info.stdout or ""
        ).strip().split("|", 2)
        mirrors = _safe_json_list(mirrors_raw)
        proxies = [
            p for p in (json.loads(http_proxy_raw or '""'), json.loads(https_proxy_raw or '""')) if p
        ]
    except (ValueError, json.JSONDecodeError):
        mirrors, proxies = [], []

    uncached_detail = "未缓存：" + ", ".join(f"{img}（{'/'.join(iids)}）" for img, iids in
                                             sorted(images.items()) if img in uncached)
    if mirrors or proxies:
        accel = mirrors + proxies
        return CheckResult(
            "base_image_readiness",
            STATUS_OK,
            f"基础镜像有未缓存项，但 daemon 已配置加速（{', '.join(accel)}）",
            detail=uncached_detail + "；已缓存：" + (", ".join(cached) or "无"),
        )

    # BUG-597：未缓存镜像若来自 ghcr.io / quay.io 等第三方 registry，Docker Hub
    # 的 registry-mirrors 帮不上忙——不能照搬 Hub 文案建议配 Hub mirror。
    uncached_registries = sorted({_image_registry(img) for img in uncached})
    hub_only = uncached_registries == ["docker.io"]
    if hub_only:
        message = (
            f"{len(uncached)} 个基础镜像未缓存，且 daemon 未配置 registry-mirrors/代理"
            "——Docker Hub 不可达网络下首次构建会超时"
        )
        suggestion = (
            "任选其一：Linux/WSL 在 /etc/docker/daemon.json 配置 registry-mirrors"
            "（国内可选 DaoCloud、阿里云等公共加速器）后重启 Docker；"
            "Docker Desktop 在 Settings→Docker Engine 配置；"
            "或为 dockerd 配置 HTTP 代理；"
            "或在可联网机器 docker pull 后 docker save | docker load 预置上述镜像"
        )
    else:
        message = (
            f"{len(uncached)} 个基础镜像未缓存，且 daemon 未配置代理"
            f"——含第三方 registry（{', '.join(uncached_registries)}），"
            "Docker Hub 的 registry-mirrors 无法覆盖，网络受限时首次构建会超时"
        )
        suggestion = (
            "Docker Hub 加速器帮不上 ghcr.io / quay.io 等第三方 registry，任选其一："
            "为 dockerd 配置 HTTP/HTTPS 代理（Linux/WSL：systemd drop-in；"
            "Docker Desktop：Settings→Resources→Proxies）；"
            "或在可联网机器 docker pull 后 docker save | docker load 预置上述镜像"
        )
    return CheckResult(
        "base_image_readiness",
        STATUS_WARN,
        message,
        detail=uncached_detail + "；已缓存：" + (", ".join(cached) or "无"),
        suggestion=suggestion,
    )


def check_git(runner: SubprocessRunner = _default_runner) -> CheckResult:
    """IMP-065（065.25）：git 可执行探测。

    缺 git 只影响 GitHub 源导入（``lwa import --from-git`` / 管理页同名入口），
    zip 与本机文件夹导入不受影响——因此是 **WARN** 而非 FAIL，不把整份
    doctor 打红。
    """
    if not shutil.which("git"):
        return CheckResult(
            "git",
            STATUS_WARN,
            "未检测到 git 可执行文件",
            detail="GitHub 源导入（IMP-065）不可用；zip / 本机文件夹导入不受影响",
            suggestion=(
                "安装 git（Ubuntu：sudo apt install git；"
                "macOS：xcode-select --install）后即可使用 GitHub 源导入"
            ),
        )
    result = runner(["git", "--version"])
    if result.returncode != 0:
        return CheckResult(
            "git",
            STATUS_WARN,
            "git 可执行但无法获取版本",
            detail=(result.stderr or "").strip()[:200] or None,
        )
    version = (result.stdout or "").strip().splitlines()
    version_line = version[0] if version else "git"
    return CheckResult(
        "git",
        STATUS_OK,
        f"git 可用（{version_line}；GitHub 源导入可用）",
    )


def check_caddy(config: Config, runner: SubprocessRunner = _default_runner) -> CheckResult:
    """Caddy 版本检查：缺失时与运行时一致，降级 builtin 并告警。"""
    if config.staticGateway != "caddy":
        return CheckResult(
            "caddy",
            STATUS_SKIP,
            f"staticGateway={config.staticGateway}，跳过 Caddy 版本检查",
        )
    if not shutil.which("caddy"):
        return CheckResult(
            "caddy",
            STATUS_WARN,
            "配置 staticGateway=caddy 但未找到 caddy，可降级 builtin 静态服务",
            suggestion=(
                f"如需 Caddy 模式，安装 Caddy ≥ {MIN_CADDY_VERSION} 并加入 PATH；"
                "否则将使用内置静态服务"
            ),
        )
    result = runner(["caddy", "version"])
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return CheckResult(
            "caddy",
            STATUS_FAIL,
            "无法获取 Caddy 版本",
            detail=stderr[:200] or None,
            suggestion=f"确认 caddy 可执行且版本 ≥ {MIN_CADDY_VERSION}",
        )
    version_line = ((result.stdout or "") + (result.stderr or "")).strip().splitlines()
    version = version_line[0] if version_line else ""
    if not version_ge(version, MIN_CADDY_VERSION):
        if not version.strip():
            # 评审-组5：空版本原产出"Caddy  不满足…"双空格消息
            return CheckResult(
                "caddy",
                STATUS_FAIL,
                "无法解析 Caddy 版本（命令无输出）",
                suggestion=f"确认 caddy 可执行且版本 ≥ {MIN_CADDY_VERSION}",
            )
        return CheckResult(
            "caddy",
            STATUS_FAIL,
            f"Caddy {version.strip()} 不满足最低要求 ≥ {MIN_CADDY_VERSION}",
            suggestion=f"升级 Caddy 至 ≥ {MIN_CADDY_VERSION}",
        )
    return CheckResult(
        "caddy",
        STATUS_OK,
        f"Caddy 可用（{version.strip()}，≥ {MIN_CADDY_VERSION}）",
    )


def check_python_packages() -> CheckResult:
    """已安装的 fastapi / uvicorn 是否满足最低版本。"""
    issues: list[str] = []
    fastapi_ver = installed_package_version("fastapi")
    if fastapi_ver is None:
        issues.append("fastapi 未安装")
    elif not version_ge(fastapi_ver, MIN_FASTAPI_VERSION):
        issues.append(f"fastapi {fastapi_ver} < {MIN_FASTAPI_VERSION}")
    uvicorn_ver = installed_package_version("uvicorn")
    if uvicorn_ver is None:
        issues.append("uvicorn 未安装")
    elif not version_ge(uvicorn_ver, MIN_UVICORN_VERSION):
        issues.append(f"uvicorn {uvicorn_ver} < {MIN_UVICORN_VERSION}")
    if issues:
        return CheckResult(
            "python_packages",
            STATUS_FAIL,
            "Python 依赖版本不满足最低要求",
            detail="；".join(issues),
            suggestion=(
                f"运行 `pip install -U 'fastapi>={MIN_FASTAPI_VERSION}' "
                f"'uvicorn>={MIN_UVICORN_VERSION}'` 或 `pip install -e .`"
            ),
        )
    return CheckResult(
        "python_packages",
        STATUS_OK,
        (
            f"fastapi {fastapi_ver}（≥ {MIN_FASTAPI_VERSION}），"
            f"uvicorn {uvicorn_ver}（≥ {MIN_UVICORN_VERSION}）"
        ),
    )


def check_port_pool(
    config: Config,
    port_in_use: PortChecker = _default_port_in_use,
    *,
    allocated_ports: set[int] | None = None,
    exclude_ports: set[int] | None = None,
) -> CheckResult:
    """WBS-26.05：端口池可用性（排除 lwa 合法自用端口）。

    抽样检查池首尾；池范围很小（≤32）时全量检查。

    建议项 H（gateway-switch-access-review）：排除 lwa **合法自用**端口——
    ``managerPort``（管理页）、``staticGatewayPort``（Caddy 别名入口）、registry
    已分配的 hostPort。这些端口被 lwa 自身监听是预期状态，报为冲突会干扰切换后
    巡检（OPS-005 / OPS-030 / OPS-031 均有误报记录）。仅当端口池范围内的**外部**
    占用（非 lwa 进程）才判 FAIL。
    """
    allocated = allocated_ports or set()
    exclude = exclude_ports or set()
    # 合法自用端口：管理端口始终自用；别名入口端口由 caddy 网关自用。
    self_ports: set[int] = {config.managerPort}
    if config.staticGatewayPort is not None:
        self_ports.add(config.staticGatewayPort)
    skip = allocated | exclude | self_ports

    conflicts: list[int] = []
    start = config.portPool.start
    end = config.portPool.end
    span = end - start + 1
    # 大范围抽样，小范围全量
    if span <= 32:
        candidates: list[int] = list(range(start, end + 1))
    else:
        candidates = [start, end, start + 1, end - 1, (start + end) // 2]
    for port in candidates:
        if port in skip:
            continue
        if port_in_use(port):
            conflicts.append(port)
    if conflicts:
        return CheckResult(
            "port_pool",
            STATUS_FAIL,
            f"端口池 {start}-{end} 存在外部占用",
            detail="被占用端口：" + ", ".join(str(p) for p in sorted(set(conflicts))),
            suggestion="修改 local-web.yml 的 portPool，或停止占用这些端口的外部进程",
        )
    self_summary = ", ".join(str(p) for p in sorted(self_ports | allocated))
    return CheckResult(
        "port_pool",
        STATUS_OK,
        f"端口池 {start}-{end}（抽样）无外部占用；已排除自用端口 {self_summary}",
    )


def check_registry(ws: Workspace) -> CheckResult:
    """WBS-26.06：SQLite registry 可读写，schema 版本正确。"""
    if not ws.db_path.is_file():
        return CheckResult(
            "registry",
            STATUS_FAIL,
            f"registry 数据库不存在：{ws.db_path}",
            suggestion="运行 `lwa init` 初始化工作区",
        )
    try:
        from local_webpage_access.registry.connection import (
            CURRENT_SCHEMA_VERSION,
            get_schema_version,
        )

        reg = Registry(ws.db_path)
        reg.open_readonly()
        try:
            version = get_schema_version(reg.conn)
            count = reg.total_count()
        finally:
            reg.close()
        if version != CURRENT_SCHEMA_VERSION:
            return CheckResult(
                "registry",
                STATUS_WARN,
                f"registry schema 版本 {version}，当前代码期望 {CURRENT_SCHEMA_VERSION}",
                suggestion="运行 `lwa init`（幂等）以应用迁移",
            )
        return CheckResult(
            "registry",
            STATUS_OK,
            f"registry 可用（schema v{version}，{count} 个实例）",
        )
    except Exception as exc:
        return CheckResult(
            "registry",
            STATUS_FAIL,
            f"registry 访问失败：{exc}",
            suggestion="若数据库损坏，备份后删除并重新 `lwa init`",
        )


def check_static_gateway(ws: Workspace) -> CheckResult:
    """WBS-26.07：静态网关目录与模板就绪。"""
    if not ws.static_gateway.is_dir():
        return CheckResult(
            "static_gateway",
            STATUS_WARN,
            f"静态网关目录不存在：{ws.static_gateway}",
            suggestion="运行 `lwa init` 创建（不影响容器实例）",
        )
    return CheckResult("static_gateway", STATUS_OK, f"静态网关目录就绪（{ws.static_gateway}）")


def _pid_alive_local(pid: int) -> bool:
    """跨平台 pid 存活探测（不依赖 psutil）。

    BUG-178：Windows 上 ``os.kill(pid, 0)`` 走 TerminateProcess 会真的杀掉进程，
    只读诊断（check_caddy_health 的 caddy.pid 存活探测）会误杀运行中的 Caddy
    master。改用 ``OpenProcess(SYNCHRONIZE)``，与 lifecycle/static_gateway 的
    ``is_pid_alive`` 实现一致；Unix 侧 ``os.kill(pid, 0)`` 仍是安全探针。
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def check_caddy_health(
    ws: Workspace,
    config: Config,
    *,
    runner: SubprocessRunner = _default_runner,
    registry: Registry | None = None,
) -> CheckResult:
    """IMP-020：Caddy 模式下的 master/admin/配置/可达性健康探针。

    仅 ``staticGateway=caddy`` 时探测（其他后端跳过）：

    1. admin :2019 是否在线（master 是否在跑）——不在线 FAIL，提示 ``lwa gateway on``；
    2. 主 Caddyfile ``caddy validate`` 是否通过——失败 FAIL，提示悬空 import（BUG-069）；
    3. ``run/caddy.pid`` 是否指向已死进程——stale 给 WARN（BUG-070）；
    4. master 在线时（提供 registry）：别名入口 ``:8080`` 与各 enabled 站点 hostPort
       可达性——不可达 WARN，提示 reload/核对站点配置。

    master 在线、配置有效、入口与站点均可达时返回 OK。
    """
    if config.staticGateway != "caddy":
        return CheckResult(
            "caddy_health",
            STATUS_SKIP,
            f"staticGateway={config.staticGateway}，跳过 Caddy 健康探针",
        )
    from local_webpage_access.static_gateway import StaticGateway

    # 不调 gateway.detect_backend()：caddy 缺失时它会 log.warning，经 RichHandler
    # 写入 stdout，污染 `lwa doctor --json` 的输出导致 JSON 不可解析（BUG-075）。
    # 此处静默判定 caddy 是否在 PATH，结果与 detect_backend 的 caddy/builtin 分支一致。
    if not shutil.which("caddy"):
        return CheckResult(
            "caddy_health",
            STATUS_WARN,
            "配置 staticGateway=caddy 但未找到 caddy，已降级 builtin",
            suggestion=f"安装 Caddy ≥ {MIN_CADDY_VERSION} 并加入 PATH 后执行 lwa gateway on",
        )
    gateway = StaticGateway(ws, config)

    findings: list[str] = []
    admin_ok = gateway._admin_alive()
    if not admin_ok:
        findings.append("admin :2019 不可达（master 未运行）")

    validate_ok = True
    main = gateway.main_config_path()
    if main.is_file():
        result = runner(["caddy", "validate", "--config", str(main), "--adapter", "caddyfile"])
        validate_ok = result.returncode == 0
        if not validate_ok:
            stderr = (result.stderr or "").strip().splitlines()
            findings.append(
                "主 Caddyfile validate 失败（可能悬空 import）"
                + (f"：{stderr[0][:160]}" if stderr else "")
            )

    stale_pid = False
    pid_path = gateway.caddy_pid_path()
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
        if pid is not None and not _pid_alive_local(pid):
            stale_pid = True

    # IMP-020：master 在线时探测别名入口 :8080 与各 enabled 站点 hostPort
    entry_unreachable = False
    site_unreachable: list[str] = []
    if admin_ok and registry is not None:
        # 别名入口 :staticGatewayPort——仅当存在路径别名时才应监听（无别名时该端口空闲）
        try:
            aliases = registry.list_route_hosts()
        except Exception:  # noqa: BLE001 — registry 不可用则跳过入口/站点探测
            aliases = {}
        entry_port = config.staticGatewayPort
        if aliases and entry_port is not None:
            # BUG-080：入口根路径 / 无路由（仅 /<alias>/ 有），必须探别名子路径，
            # 否则恒 404 误报 WARN。
            # 评审-组5：原只探首别名，部分别名 404（reload 未生效/片段缺失）会漏报；
            # 改为全量探测（别名多于 5 个时截断，防探测成本失控）。
            for probe_alias in list(aliases)[:5]:
                if not gateway.health_check(int(entry_port), path=f"/{probe_alias}/"):
                    entry_unreachable = True
                    findings.append(
                        f"别名入口 :{entry_port}/{probe_alias}/ 不可达"
                        f"（入口未就绪或该别名 reload 未生效）"
                    )
        # 各 enabled 静态站点 hostPort
        try:
            rows = registry.list_instances()
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            if row.get("runtime") != "shared-static":
                continue
            iid = row["id"]
            site = registry.get_static_site(iid)
            if not site or not site.get("enabled"):
                continue
            hp = site.get("host_port")
            if hp and not gateway.health_check(int(hp)):
                site_unreachable.append(f"{iid}:{hp}")
        if site_unreachable:
            preview = ", ".join(site_unreachable[:5])
            more = f" 等 {len(site_unreachable)} 个" if len(site_unreachable) > 5 else ""
            findings.append(f"enabled 站点 hostPort 不可达：{preview}{more}")

    if not admin_ok:
        return CheckResult(
            "caddy_health",
            STATUS_FAIL,
            "；".join(findings),
            suggestion="执行 `lwa gateway on` 启动 Caddy master；"
            "若反复失败，检查 static-gateway/sites 与主 Caddyfile 是否含悬空 import（BUG-069）",
        )
    if not validate_ok:
        return CheckResult(
            "caddy_health",
            STATUS_FAIL,
            "；".join(findings),
            suggestion="主 Caddyfile 非法：执行 `lwa gateway off` 再 `lwa gateway on`，"
            "或核对 sites/ 与主配置 import 一致性",
        )
    if entry_unreachable or site_unreachable:
        return CheckResult(
            "caddy_health",
            STATUS_WARN,
            "；".join(findings),
            suggestion="master 在线但部分入口/站点不可达：执行 `lwa gateway off` 再 "
            "`lwa gateway on`，或对不可达实例 `lwa restart <id>` 触发 reload",
        )
    if stale_pid:
        return CheckResult(
            "caddy_health",
            STATUS_WARN,
            "Caddy master 在线、配置有效，但 run/caddy.pid 指向已死进程（stale）",
            suggestion="执行 `lwa gateway off` 后 `lwa gateway on` 清理 stale pid（BUG-070）",
        )
    return CheckResult(
        "caddy_health",
        STATUS_OK,
        "Caddy master 在线（admin :2019），主配置 validate 通过",
    )


# ---- 建议项 F：切换交接与地址漂移诊断（gateway-switch-access-review）----------


def _list_listeners(port: int) -> list[tuple[str, str]]:
    """best-effort：用 lsof 列出端口监听者 ``[(name, pid_str), ...]``。

    POSIX 上 lsof 可用；Windows / 无 lsof 时返回空列表（调用方据此 SKIP）。
    """
    if shutil.which("lsof") is None:
        return []
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    listeners: list[tuple[str, str]] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            listeners.append((parts[0], parts[1]))
    return listeners


# ---- DEV-094：工作区路径一致性（裸 mv 后残留诊断）---------------------------


_CADDY_BACKTICK_PATH = re.compile(r"`([^`]+)`")
_CADDY_DOUBLE_QUOTED_PATH = re.compile(r'"((?:\\.|[^"\\])*)"')


def _is_local_filesystem_path(raw: str) -> bool:
    """判断 Caddy 引号内字符串是否像本地绝对路径（非 URL）。"""
    s = raw.strip()
    if not s:
        return False
    lower = s.lower()
    if lower.startswith(("http://", "https://", "file:")):
        return False
    # Unix 绝对路径，或 Windows 盘符路径（含正斜杠形式）
    if s.startswith("/"):
        return True
    if len(s) >= 3 and s[1] == ":" and s[0].isalpha() and s[2] in "/\\":
        return True
    return False


_CADDY_OUTPUT_FILE_LINE = re.compile(r"^\s*output\s+file\s+", re.MULTILINE)


def _extract_caddy_local_paths(text: str) -> list[str]:
    """从 Caddyfile / 片段文本提取反引号或双引号包裹的本地路径。"""
    found: list[str] = []
    seen: set[str] = set()
    for match in _CADDY_BACKTICK_PATH.finditer(text):
        raw = match.group(1)
        if _is_local_filesystem_path(raw) and raw not in seen:
            seen.add(raw)
            found.append(raw)
    for match in _CADDY_DOUBLE_QUOTED_PATH.finditer(text):
        raw = match.group(1).replace('\\"', '"').replace("\\\\", "\\")
        if _is_local_filesystem_path(raw) and raw not in seen:
            seen.add(raw)
            found.append(raw)
    return found


def _is_caddy_runtime_created_path(text: str, ref: str) -> bool:
    """判断 Caddy 片段中的路径是否由 Caddy 运行时按需创建（不应要求预先存在）。

    ``log { output file <path> { ... } }`` 指向的日志文件由 Caddy 在首次写
    日志时自动创建（``static_gateway.generate_site_config`` 与主 Caddyfile
    统一入口块都会写入 ``<workspace>/logs/static-access.log``）。全新工作区或
    网关刚生成配置但尚无访问时该文件不存在属正常；若对其做
    ``Path.exists()`` 校验会误报“引用路径不存在”（BUG-428）。此类路径只需
    校验落在当前工作区内、且父目录可创建/写入即可。

    注意必须扫描**所有**出现位置：``generate_site_config`` 会在指令行之前
    写入 ``# 渲染变量：…`` 注释头，其中也含同一日志路径。若只看首次出现
    （BUG-428），命中的是注释行而非 ``output file`` 指令行，豁免失效。
    """
    start = 0
    while True:
        idx = text.find(ref, start)
        if idx == -1:
            return False
        # 取该路径所在行的前缀（到行首），判断是否以 `output file` 指令开头
        line_start = text.rfind("\n", 0, idx) + 1
        prefix = text[line_start:idx]
        if _CADDY_OUTPUT_FILE_LINE.match(prefix):
            return True
        start = idx + len(ref)


def _caddy_fragment_texts(ws: Workspace) -> list[tuple[str, str]]:
    """返回 [(label, text), ...]：主 Caddyfile + sites/*.conf + aliases/*.conf。"""
    out: list[tuple[str, str]] = []
    main = ws.static_gateway / "Caddyfile"
    if main.is_file():
        try:
            out.append((str(main), main.read_text(encoding="utf-8")))
        except OSError:
            pass
    for folder in (ws.static_sites, ws.static_aliases):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.conf")):
            try:
                out.append((str(path), path.read_text(encoding="utf-8")))
            except OSError:
                continue
    return out


def _path_field_mismatch(
    instance_id: str,
    field: str,
    actual: str | None,
    expected: str,
    *,
    source: str,
) -> str | None:
    """若 actual 存在且与期望不等，返回一行诊断；否则 None。"""
    if actual is None or actual == "":
        return None
    if actual == expected:
        return None
    return f"{instance_id} {source}.{field}: actual={actual} expected={expected}"


def check_workspace_path_consistency(
    ws: Workspace,
    config: Config,
    registry: Registry | None = None,
) -> CheckResult:
    """DEV-094：只读检查工作区派生路径、Caddy 引用与 SQLite data 挂载一致性。

    聚合为单一 ``workspace_path_consistency`` 结果：
    * 活跃 manifest/registry 的可确定派生字段 vs 当前 workspace 规范值；
    * 主 Caddyfile / sites / aliases 片段中本地路径必须在当前工作区内且存在
      （旧路径被 Docker 自动重建后仍"存在"，仅查存在性会漏报，BUG-426）；
    * Docker 可用时，SQLite 实例 LWA 管理的 data bind mount Source 是否漂移。

    历史 builds/events 与合法外部 ``sourceZipPath`` 不告警。Docker 不可用、
    挂载观测失败、或 registry 不可用/读取失败（BUG-430）时对应子项记 SKIP
    （无其他发现时整体 STATUS_SKIP，BUG-427），不把整个 doctor 判 FAIL。
    """
    from local_webpage_access.compose import _is_sqlite, container_data_paths
    from local_webpage_access.docker_runtime import DockerError, DockerRuntime
    from local_webpage_access.hosting import expected_workspace_derived_paths
    from local_webpage_access.models import InstanceManifest

    findings: list[str] = []
    mount_notes: list[str] = []

    # ---- 1) manifest / registry 派生字段 ---------------------------------
    # BUG-430：registry 不可用（None 或读取失败）时 rows 为空会漏掉全部
    # manifest/registry 字段比对与数据挂载检查——必须记 SKIP 而非静默假绿。
    rows: list[dict[str, Any]] = []
    registry_note: str | None = None
    if registry is None:
        registry_note = (
            "registry 不可用（未提供），manifest/registry 字段与数据挂载检查未完成（SKIP）"
        )
    else:
        try:
            rows = registry.list_instances()
        except Exception as exc:  # noqa: BLE001 — 只读诊断，registry 异常不阻断
            rows = []
            registry_note = (
                f"registry 读取失败，manifest/registry 字段与数据挂载检查未完成（SKIP）：{exc}"
            )
    if registry_note is not None:
        mount_notes.append(registry_note)

    for row in rows:
        iid = row["id"]
        expected = expected_workspace_derived_paths(ws, iid)
        manifest: InstanceManifest | None = None
        manifest_path = ws.app_manifest_path(iid)
        if manifest_path.is_file():
            try:
                manifest = InstanceManifest.load(manifest_path)
            except Exception:  # noqa: BLE001
                findings.append(f"{iid} manifest: 解析失败，跳过派生字段比对")
                continue

        if manifest is not None:
            miss = _path_field_mismatch(
                iid,
                "appPath",
                manifest.appPath,
                expected["appPath"],
                source="manifest",
            )
            if miss:
                findings.append(miss)
            if manifest.container is not None:
                miss = _path_field_mismatch(
                    iid,
                    "composePath",
                    manifest.container.composePath,
                    expected["composePath"],
                    source="manifest",
                )
                if miss:
                    findings.append(miss)
                miss = _path_field_mismatch(
                    iid,
                    "dockerfilePath",
                    manifest.container.dockerfilePath,
                    expected["dockerfilePath"],
                    source="manifest",
                )
                if miss:
                    findings.append(miss)
            if manifest.static is not None:
                miss = _path_field_mismatch(
                    iid,
                    "gatewayConfigPath",
                    manifest.static.gatewayConfigPath,
                    expected["gatewayConfigPath"],
                    source="manifest",
                )
                if miss:
                    findings.append(miss)

        # registry 侧同名字段（snake_case）
        if registry is not None:
            inst = registry.get_instance(iid) or row
            miss = _path_field_mismatch(
                iid,
                "appPath",
                inst.get("app_path"),
                expected["appPath"],
                source="registry",
            )
            if miss:
                findings.append(miss)
            crec = registry.get_container(iid)
            if crec:
                miss = _path_field_mismatch(
                    iid,
                    "composePath",
                    crec.get("compose_path"),
                    expected["composePath"],
                    source="registry",
                )
                if miss:
                    findings.append(miss)
                miss = _path_field_mismatch(
                    iid,
                    "dockerfilePath",
                    crec.get("dockerfile_path"),
                    expected["dockerfilePath"],
                    source="registry",
                )
                if miss:
                    findings.append(miss)
            srec = registry.get_static_site(iid)
            if srec:
                miss = _path_field_mismatch(
                    iid,
                    "gatewayConfigPath",
                    srec.get("gateway_config_path"),
                    expected["gatewayConfigPath"],
                    source="registry",
                )
                if miss:
                    findings.append(miss)

    # ---- 2) Caddy 本地路径引用：必须在当前工作区内且存在 -------------------
    ws_root = ws.root
    for label, text in _caddy_fragment_texts(ws):
        for ref in _extract_caddy_local_paths(text):
            ref_path = Path(ref)
            try:
                resolved = ref_path.resolve()
            except OSError:
                resolved = ref_path
            if not resolved.is_relative_to(ws_root):
                # BUG-426：旧路径可能仍然存在（如 Docker 自动重建的旧工作区），
                # 仅查存在性会漏报——必须按当前工作区判定规范归属。
                findings.append(
                    f"caddy {label}: 引用路径不在当前工作区 actual={ref} expected={ws_root} 之内"
                )
                continue
            # BUG-428：``log { output file <path> }`` 指向的日志文件由 Caddy
            # 运行时按需创建（全新工作区/无访问时尚不存在属正常），不应要求
            # 文件本身预先存在；只校验父目录落在工作区内（上面已校验整体在
            # 工作区内）即可。其余路径（root/import 等）必须预先存在。
            if _is_caddy_runtime_created_path(text, ref):
                continue
            if not ref_path.exists():
                findings.append(
                    f"caddy {label}: 引用路径不存在 "
                    f"actual={ref} expected=当前工作区内存在的本地路径"
                )

    # ---- 3) SQLite data bind mount（Docker 可用时）------------------------
    docker_ok = False
    try:
        docker_ok = bool(DockerRuntime.is_available())
    except Exception:  # noqa: BLE001
        docker_ok = False

    if not docker_ok:
        has_sqlite = False
        for row in rows:
            mp = ws.app_manifest_path(row["id"])
            if not mp.is_file():
                continue
            try:
                m = InstanceManifest.load(mp)
            except Exception:  # noqa: BLE001
                continue
            if _is_sqlite(m):
                has_sqlite = True
                break
        if has_sqlite:
            mount_notes.append("data mount: Docker 不可用，跳过挂载漂移检查（SKIP）")
    else:
        runtime = DockerRuntime(ws, registry)
        for row in rows:
            iid = row["id"]
            mp = ws.app_manifest_path(iid)
            if not mp.is_file():
                continue
            try:
                manifest = InstanceManifest.load(mp)
            except Exception:  # noqa: BLE001
                continue
            if not _is_sqlite(manifest):
                continue
            try:
                mounts = runtime.bind_mounts(iid, all_containers=True)
            except DockerError as exc:
                mount_notes.append(f"{iid} data mount: 观测失败，跳过（SKIP）：{exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                mount_notes.append(f"{iid} data mount: 观测失败，跳过（SKIP）：{exc}")
                continue
            expected_src = ws.app_data(iid).resolve()
            destinations = set(container_data_paths(ws.app_current(iid), manifest))
            managed = [m for m in mounts if m.destination in destinations]
            # 评审-组5：data bind mount 整体缺失（volume 丢失/未挂载）此前静默
            # OK；destinations 非空而 managed 为空时显式告警
            if destinations and not managed:
                findings.append(
                    f"{iid} dataMount: 期望的 data 挂载（{'、'.join(sorted(destinations))}）"
                    f"在容器观测中整体缺失——数据库可能指向容器内临时层，重建会丢数据"
                )
            for mount in managed:
                actual = Path(mount.source).resolve() if mount.source else None
                if actual != expected_src:
                    findings.append(
                        f"{iid} dataMount[{mount.destination}]: "
                        f"actual={mount.source} expected={expected_src}"
                    )

    # ---- 聚合结果 --------------------------------------------------------
    detail_parts = findings + mount_notes
    detail = "\n".join(detail_parts) if detail_parts else None
    suggestion = (
        "优先运行 `lwa workspace relocate --verify` 核对迁移；"
        "若已裸 mv 到新路径，对受影响实例执行 `lwa rebuild <id>` / "
        "`lwa recover`（或 gateway on）修复派生路径与挂载"
    )
    if findings:
        return CheckResult(
            "workspace_path_consistency",
            STATUS_WARN,
            f"发现 {len(findings)} 处工作区路径不一致",
            detail=detail,
            suggestion=suggestion,
        )
    if mount_notes:
        # BUG-427/BUG-430：挂载检查或 registry 读取未完成不得报 OK——
        # JSON/自动化消费者会误以为已验证。返回 SKIP 并附未完成原因。
        return CheckResult(
            "workspace_path_consistency",
            STATUS_SKIP,
            "已完成项未见不一致；部分检查未完成（SKIP）",
            detail=detail,
            suggestion="待 registry / Docker 可用后重跑 lwa doctor，以完成全部一致性检查",
        )
    return CheckResult(
        "workspace_path_consistency",
        STATUS_OK,
        "工作区派生路径、Caddy 引用与数据挂载一致",
    )


def check_lan_url_stale(ws: Workspace, config: Config, registry: Registry) -> CheckResult:
    """建议 F / G1：检测实例 lanUrl 是否指向失效（漂移）的 LAN IP。

    换 Wi-Fi / DHCP 续约后本机 LAN IP 变化，但各实例 ``local-web.json`` 的
    ``lanUrl`` 仅在 start/enable 时写入，不会自愈 → 管理页链接失效。本检查比对
    各实例 lanUrl host 与当前 :func:`resolve_lan_ip`（及 127.0.0.1），漂移则 WARN，
    提示 ``lwa access refresh``。
    """
    from local_webpage_access.ports import resolve_lan_ip

    lan_ip = resolve_lan_ip(config)
    if lan_ip is None:
        # BUG-358：无法解析本机 LAN IP 时不得静默 OK（有 lanUrl 则无法确认是否仍有效）
        has_lan_url = False
        for row in registry.list_instances():
            manifest_path = ws.app_manifest_path(row["id"])
            if not manifest_path.is_file():
                continue
            try:
                from local_webpage_access.models import InstanceManifest

                manifest = InstanceManifest.load(manifest_path)
            except Exception:  # noqa: BLE001
                continue
            if manifest.network and manifest.network.lanUrl:
                has_lan_url = True
                break
        if has_lan_url:
            return CheckResult(
                "lan_url_stale",
                STATUS_WARN,
                "无法解析当前 LAN IP，不能确认实例 lanUrl 是否仍有效",
                suggestion="检查网络后重试；恢复后运行 `lwa access refresh`",
            )
        return CheckResult(
            "lan_url_stale",
            STATUS_SKIP,
            "无法解析当前 LAN IP，且无实例 lanUrl 需核对",
        )
    drifted_ids, skipped = _collect_lan_drifted_ids(ws, registry, lan_ip)
    drifted = [f"{iid}({host})" for iid, host in drifted_ids]
    if drifted:
        return CheckResult(
            "lan_url_stale",
            STATUS_WARN,
            f"{len(drifted)} 个实例的 lanUrl 指向非当前 LAN IP（{lan_ip}）",
            detail="漂移实例：" + ", ".join(drifted[:8]),
            suggestion="运行 `lwa access refresh` 用当前 LAN IP 刷新所有实例访问地址",
        )
    return CheckResult(
        "lan_url_stale",
        STATUS_OK,
        f"实例 lanUrl 与当前 LAN IP（{lan_ip or '127.0.0.1'}）一致"
        + ("" if not skipped else f"（{skipped} 个 manifest 跳过）"),
    )


def _collect_lan_drifted_ids(
    ws: Workspace, registry: Registry, lan_ip: str | None
) -> tuple[list[tuple[str, str]], int]:
    """返回 ([(instance_id, host), ...], skipped_count)。"""
    drifted: list[tuple[str, str]] = []
    skipped = 0
    for row in registry.list_instances():
        iid = row["id"]
        manifest_path = ws.app_manifest_path(iid)
        if not manifest_path.is_file():
            continue
        try:
            from local_webpage_access.models import InstanceManifest

            manifest = InstanceManifest.load(manifest_path)
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        lan_url = manifest.network.lanUrl if manifest.network else None
        if not lan_url:
            continue
        host = _url_host(lan_url)
        if lan_ip and host and host not in (lan_ip, "127.0.0.1"):
            drifted.append((iid, host))
    return drifted, skipped


def check_source_freshness(ws: Workspace) -> CheckResult:
    """issue #8：源码新鲜度检查（WARN 级，纯离线）。

    * folder 源：复用 :func:`lifecycle.check_folder_source_staleness` 比对
      源目录指纹与上次同步指纹，漂移 / 源目录丢失 → WARN；
    * git 源：doctor 不触网，SKIP 并提示用 ``lwa rebuild <id>`` 触发在线检测；
    * zip 源：无上游，不参与。
    """
    from local_webpage_access.lifecycle import check_folder_source_staleness
    from local_webpage_access.models import InstanceManifest

    apps_root = ws.apps
    if not apps_root.is_dir():
        return CheckResult("source_freshness", STATUS_SKIP, "无实例目录")
    stale: list[str] = []
    missing: list[str] = []
    git_count = 0
    for manifest_path in sorted(apps_root.glob("*/local-web.json")):
        try:
            manifest = InstanceManifest.load(manifest_path)
        except Exception:  # noqa: BLE001 — 损坏 manifest 由其它检查报告
            continue
        source_kind = getattr(manifest, "sourceKind", "zip")
        if source_kind == "git":
            git_count += 1
            continue
        if source_kind != "folder":
            continue
        if check_folder_source_staleness(manifest) is None:
            continue
        source_dir = getattr(manifest, "sourceDirPath", None)
        if source_dir and not Path(source_dir).is_dir():
            missing.append(manifest.id)
        else:
            stale.append(manifest.id)
    problems = [f"{iid}（源码已变更）" for iid in stale] + [
        f"{iid}（源目录丢失）" for iid in missing
    ]
    if problems:
        return CheckResult(
            "source_freshness",
            STATUS_WARN,
            f"{len(problems)} 个 folder 源实例的源码与 current/ 不一致",
            detail="陈旧实例：" + ", ".join(problems[:8]),
            suggestion="运行 `lwa rebuild --sync <id>` 或 `lwa import --from-dir --update <id>` 同步源码后重建",
        )
    if git_count:
        return CheckResult(
            "source_freshness",
            STATUS_SKIP,
            f"{git_count} 个 git 源实例不做离线探测（运行 `lwa rebuild <id>` 时会在线检测并警告）",
        )
    return CheckResult(
        "source_freshness",
        STATUS_OK,
        "folder 源实例源码与 current/ 一致（zip 源无上游，不参与）",
    )


def check_backend_handoff(ws: Workspace, config: Config, registry: Registry) -> CheckResult:
    """建议 F / G3：检测 builtin 与 caddy 在同一 hostPort 上双开（切换残留）。

    切换 builtin↔caddy 时若旧进程未停干净（建议 A 前的现场已观察到），同一
    hostPort 会同时被 Python ``http.server`` 与 Caddy 监听，行为不确定、排障极难。
    用 lsof 检查每个 enabled 静态站点的 hostPort，发现双开则 FAIL。无 lsof 时 SKIP。
    """
    double: list[str] = []
    probed = 0
    for row in registry.list_instances():
        if row.get("runtime") != "shared-static":
            continue
        iid = row["id"]
        site = registry.get_static_site(iid)
        if not site or not site.get("enabled"):
            continue
        hp = site.get("host_port")
        if not hp:
            continue
        listeners = _list_listeners(int(hp))
        if not listeners:
            continue
        probed += 1
        names = {name.lower() for name, _ in listeners}
        has_caddy = any("caddy" in n for n in names)
        has_python = any("python" in n or "http.server" in n for n in names)
        if has_caddy and has_python:
            double.append(f"{iid}:{hp}（{', '.join(sorted(names))}）")
    if probed == 0:
        return CheckResult(
            "backend_handoff",
            STATUS_SKIP,
            "无 enabled 静态站点 hostPort 可探（或 lsof 不可用）",
        )
    if double:
        return CheckResult(
            "backend_handoff",
            STATUS_FAIL,
            f"{len(double)} 个 hostPort 上 builtin + caddy 双开（切换未彻底交接）",
            detail="双开端口：" + ", ".join(double),
            suggestion="运行 `lwa gateway off` 再 `lwa gateway on`，统一停掉残留进程后重启网关",
        )
    return CheckResult(
        "backend_handoff",
        STATUS_OK,
        f"已探 {probed} 个 enabled 静态 hostPort，未发现 builtin+caddy 双开",
    )


def check_port_contention(
    ws: Workspace, config: Config, *, registry: Registry | None = None
) -> CheckResult:
    """建议 F / §2.7：检测关键端口上的**非预期**监听者（测试/外部孤儿）。

    陈旧监听不只来自 builtin↔caddy 切换，也可能来自 pytest 泄漏的真实 Caddy
    占 ``:2019``（现场 pid 75224，见复盘 §2.7）。本检查断言关键端口上的监听者
    符合当前后端与工作区预期：

    * ``:2019``（admin）：若有监听者但**非** ``run/caddy.pid`` 所记 master，判 FAIL（孤儿）；
    * ``:staticGatewayPort``（别名入口）：caddy 后端下应仅 caddy 监听。

    无 lsof 时 SKIP（无法判定监听者身份）。

    仅在 ``staticGateway=caddy`` 时检查——:2019 / 别名入口都是 caddy 的端口，
    builtin 模式工作区不占用它们，其上的陈旧监听不属本工作区 concern（避免 builtin
    工作区因机器上残留的测试 caddy 而 doctor FAIL）。builtin+caddy 双开由
    :func:`check_backend_handoff` 负责。
    """
    from local_webpage_access.static_gateway import StaticGateway

    if config.staticGateway != "caddy":
        return CheckResult(
            "port_contention",
            STATUS_SKIP,
            f"staticGateway={config.staticGateway}，:2019/别名入口非本工作区占用，跳过",
        )
    findings: list[str] = []
    probed = 0
    gateway = StaticGateway(ws, config)
    # :2019 admin
    admin_listeners = _list_listeners(ADMIN_DOCTOR_PORT)
    caddy_pid = None
    pid_path = gateway.caddy_pid_path()
    if pid_path.is_file():
        try:
            caddy_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            caddy_pid = None
    if admin_listeners:
        probed += 1
        non_self = [
            (name, pid)
            for name, pid in admin_listeners
            if caddy_pid is None or pid != str(caddy_pid)
        ]
        if non_self and not gateway._admin_alive():
            # :2019 被占但非本工作区 caddy master（admin 不可达说明不是健康 master）
            findings.append(
                f":2019 被 {len(non_self)} 个非预期进程占用"
                f"（{', '.join(sorted({n for n, _ in non_self}))}）——疑似测试/外部孤儿"
            )
        elif non_self:
            findings.append(
                f":2019 存在非本工作区 caddy.pid 记录的监听者"
                f"（{', '.join(sorted({n for n, _ in non_self}))}）"
            )
    # :staticGatewayPort（别名入口）：caddy 后端下应仅 caddy 监听。
    # 只要存在非 caddy 监听者即 FAIL（含 caddy+python 混合），不得因「有 caddy」放行。
    entry_port = config.staticGatewayPort
    if entry_port is not None:
        entry_listeners = _list_listeners(int(entry_port))
        if entry_listeners:
            probed += 1
            non_caddy = sorted(
                {name.lower() for name, _ in entry_listeners if "caddy" not in name.lower()}
            )
            if non_caddy:
                findings.append(
                    f":{entry_port}（别名入口）存在非 caddy 监听者（{', '.join(non_caddy)}）"
                )
    if probed == 0:
        return CheckResult(
            "port_contention",
            STATUS_SKIP,
            "关键端口无监听者或 lsof 不可用，跳过",
        )
    if findings:
        return CheckResult(
            "port_contention",
            STATUS_FAIL,
            "；".join(findings),
            suggestion="确认监听者来源：测试泄漏用 pkill 清理；外部进程改用其他端口；"
            "本工作区用 `lwa gateway off` 再 `lwa gateway on`",
        )
    return CheckResult(
        "port_contention",
        STATUS_OK,
        f"关键端口（:2019{f'/:{entry_port}' if entry_port else ''}）监听者符合预期",
    )


def _url_host(url: str) -> str | None:
    """从 URL 提取 host（供 lan_url_stale 比对）。"""
    from urllib.parse import urlparse

    return urlparse(url).hostname


def check_disk_space(ws: Workspace, *, min_gb: float = 1.0) -> CheckResult:
    """WBS-26.08：工作区所在磁盘剩余空间。"""
    try:
        usage = shutil.disk_usage(str(ws.root))
    except OSError as exc:
        return CheckResult(
            "disk_space",
            STATUS_SKIP,
            f"无法获取磁盘信息：{exc}",
        )
    free_gb = usage.free / (1024**3)
    if free_gb < min_gb:
        return CheckResult(
            "disk_space",
            STATUS_FAIL,
            f"磁盘剩余 {free_gb:.2f} GB，低于阈值 {min_gb} GB",
            detail=f"total={usage.total / 1024**3:.1f}GB used={usage.used / 1024**3:.1f}GB",
            suggestion="清理工作区 inbox/ 与 logs/，或迁移工作区到更大磁盘",
        )
    if free_gb < min_gb * 3:
        return CheckResult(
            "disk_space",
            STATUS_WARN,
            f"磁盘剩余 {free_gb:.2f} GB，接近阈值",
            suggestion="关注磁盘占用增长",
        )
    return CheckResult("disk_space", STATUS_OK, f"磁盘剩余 {free_gb:.2f} GB（充足）")


def check_memory() -> CheckResult:
    """WBS-26.09：内存与 swap（跨平台尽力检测）。"""
    try:
        import psutil

        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        total_gb = mem.total / 1024**3
        avail_gb = mem.available / 1024**3
        if avail_gb < 0.2:
            return CheckResult(
                "memory",
                STATUS_FAIL,
                f"可用内存仅 {avail_gb:.2f} GB",
                detail=f"total={total_gb:.1f}GB swap={swap.total / 1024**3:.1f}GB",
                suggestion="停止部分实例或增加 swap",
            )
        return CheckResult(
            "memory",
            STATUS_OK,
            f"内存可用 {avail_gb:.1f} / {total_gb:.1f} GB",
        )
    except ImportError:
        pass

    # 回退：Linux /proc/meminfo
    if sys.platform.startswith("linux"):
        try:
            info: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.strip().split()[0]) * 1024
            # 评审-组5：内核 <3.14/精简容器无 MemAvailable，原直接得 0 误报
            # FAIL"可用内存 0.00GB"；回退 MemFree，仍缺失走 SKIP 分支
            avail = info.get("MemAvailable") or info.get("MemFree") or 0
            total = info.get("MemTotal", 0)
            if not avail:
                # 评审-组5：MemAvailable 与 MemFree 均缺失（极端精简内核）时
                # 无法判定，SKIP 而非按 0GB 误报 FAIL
                return CheckResult(
                    "memory",
                    STATUS_SKIP,
                    "/proc/meminfo 缺少 MemAvailable/MemFree，跳过内存检查",
                )
            avail_gb = avail / 1024**3
            total_gb = total / 1024**3
            if avail_gb < 0.2:
                return CheckResult(
                    "memory",
                    STATUS_FAIL,
                    f"可用内存仅 {avail_gb:.2f} GB（/proc/meminfo）",
                    suggestion="停止部分实例或增加 swap",
                )
            return CheckResult(
                "memory",
                STATUS_OK,
                f"内存可用 {avail_gb:.1f} / {total_gb:.1f} GB（/proc/meminfo）",
            )
        except OSError:
            pass

    return CheckResult(
        "memory",
        STATUS_SKIP,
        f"无法检测内存（{platform.system()} 无 psutil）",
        suggestion="pip install psutil 以启用内存检查",
    )


# ---- 单实例诊断（WBS-26.10/11）---------------------------------------------


def diagnose_instance(ws: Workspace, registry: Registry, instance_id: str) -> list[CheckResult]:
    """WBS-26.10：对单个实例执行健康诊断，返回检查项列表。"""
    from local_webpage_access.models import InstanceManifest
    from local_webpage_access.paths import validate_instance_id

    results: list[CheckResult] = []

    # 0. id 合法性（BUG-025）
    try:
        validate_instance_id(instance_id)
    except Exception as exc:
        results.append(
            CheckResult(
                f"instance:{instance_id}",
                STATUS_FAIL,
                f"实例 id 非法：{exc}",
                suggestion="实例 id 仅允许小写字母、数字、短横线",
            )
        )
        return results

    # 1. registry 中存在
    if not registry.instance_exists(instance_id):
        results.append(
            CheckResult(
                f"instance:{instance_id}",
                STATUS_FAIL,
                f"实例 {instance_id} 不在 registry",
                suggestion="确认 id 正确，或运行 `lwa list` 查看全部实例",
            )
        )
        return results

    # 2. manifest 文件
    manifest_path = ws.app_manifest_path(instance_id)
    if not manifest_path.is_file():
        results.append(
            CheckResult(
                f"instance:{instance_id}:manifest",
                STATUS_FAIL,
                f"manifest 缺失：{manifest_path}",
                suggestion="manifest 丢失，建议 remove 后重新导入",
            )
        )
    else:
        try:
            manifest = InstanceManifest.load(manifest_path)
            results.append(
                CheckResult(
                    f"instance:{instance_id}:manifest",
                    STATUS_OK,
                    f"manifest 完整（kind={manifest.kind}）",
                )
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    f"instance:{instance_id}:manifest",
                    STATUS_FAIL,
                    f"manifest 解析失败：{exc}",
                    suggestion=f"检查 {manifest_path} 是否为合法 JSON",
                )
            )

    # 3. 实例目录
    app_dir = ws.app_dir(instance_id)
    if not app_dir.is_dir():
        results.append(
            CheckResult(
                f"instance:{instance_id}:files",
                STATUS_FAIL,
                f"实例目录缺失：{app_dir}",
                suggestion="文件丢失，建议 remove 后重新导入",
            )
        )
    else:
        results.append(
            CheckResult(
                f"instance:{instance_id}:files",
                STATUS_OK,
                f"实例目录就绪（{app_dir}）",
            )
        )

    # 4. 状态与最近错误
    status_row = registry.get_instance(instance_id)
    if status_row:
        status = status_row.get("status") or "?"
        last_error = status_row.get("last_error")
        desired = status_row.get("desired_state") or "?"
        if status == "failed":
            results.append(
                CheckResult(
                    f"instance:{instance_id}:status",
                    STATUS_FAIL,
                    f"实例状态 failed（期望 {desired}）",
                    detail=last_error or None,
                    suggestion="查看 logs/ 下的 run.log 与 build 日志；"
                    "可调用对应 skill 排障后 `lwa restart`",
                )
            )
        elif status == "pending":
            results.append(
                CheckResult(
                    f"instance:{instance_id}:status",
                    STATUS_WARN,
                    "实例 pending（未识别或未启动）",
                    suggestion="确认来源可信后 `lwa start`，或用 skill 补全配置",
                )
            )
        else:
            results.append(
                CheckResult(
                    f"instance:{instance_id}:status",
                    STATUS_OK,
                    f"实例状态 {status}（期望 {desired}）",
                )
            )

    # 5. 最近事件
    events = registry.list_events(instance_id, limit=5)
    if events:
        recent = events[0]
        results.append(
            CheckResult(
                f"instance:{instance_id}:events",
                STATUS_OK,
                f"最近事件：[{recent['event_type']}] {recent['message'][:80]}",
            )
        )

    # 6. 日志文件存在性
    run_log = ws.app_logs(instance_id) / "run.log"
    if run_log.is_file():
        size = run_log.stat().st_size
        results.append(
            CheckResult(
                f"instance:{instance_id}:logs",
                STATUS_OK,
                f"运行日志存在（{size} 字节）：{run_log}",
            )
        )
    else:
        results.append(
            CheckResult(
                f"instance:{instance_id}:logs",
                STATUS_WARN,
                f"未找到运行日志：{run_log}",
                suggestion="实例可能从未启动；运行 `lwa start {instance_id}`",
            )
        )

    # 7. 兼容性预检发现（IMP-056 Gate-2，B.06）
    try:
        manifest = InstanceManifest.load(manifest_path)
        findings = getattr(manifest, "compatibilityFindings", [])
        if findings:
            critical = [f for f in findings if f.severity == "critical"]
            warning = [f for f in findings if f.severity == "warning"]
            parts = []
            if critical:
                parts.append(f"{len(critical)} critical")
            if warning:
                parts.append(f"{len(warning)} warning")
            info_count = len(findings) - len(critical) - len(warning)
            if info_count > 0:
                parts.append(f"{info_count} info")
            summary = "、".join(parts)
            detail_lines = []
            for f in findings[:10]:
                loc = f" ({f.file}:{f.line})" if f.file else ""
                detail_lines.append(f"[{f.checkId}/{f.severity}] {f.title}{loc}")
            results.append(
                CheckResult(
                    f"instance:{instance_id}:compatibility",
                    STATUS_WARN if critical else STATUS_OK,
                    f"兼容性预检：{summary}（不阻断 / 以 IMP-055 为准）",
                    detail="\n".join(detail_lines) if detail_lines else None,
                    suggestion="参考各 finding 的 fix 建议；设别名时仍以 IMP-055 运行时探测为准",
                )
            )
    except Exception:  # noqa: BLE001 - 兼容性检查不阻断诊断
        pass

    return results


# ---- 自有服务运行态与重启韧性（IMP-060）-------------------------------------


# 事故实证修复命令（§14.1：Ubuntu 裸进程重启后丢失半天的现场修复）
_RECOMMENDED_INSTALL_CMD_LINUX = "lwa autostart install --with-caddy --linger"
_RECOMMENDED_INSTALL_CMD_MACOS = "lwa autostart install --with-caddy"

_SERVICE_START_CMD = {
    "daemon": "lwa daemon on",
    "manager": "lwa manager on",
    "gateway": "lwa gateway on",
}


def _service_observed_running(name: str, ws: Workspace, config: Config) -> bool | None:
    """观测指定服务是否运行；探测异常返回 ``None``（不升 FAIL，只降 detail）。"""
    try:
        if name == "daemon":
            from local_webpage_access import daemon as daemon_mod

            return bool(daemon_mod.is_running(ws))
        if name == "manager":
            from local_webpage_access import manager_service

            return bool(manager_service.is_running(ws, config))
        from local_webpage_access import gateway_service

        return bool(gateway_service.is_gateway_running(ws, config))
    except Exception:  # noqa: BLE001 — 探测异常按未知处理，不误报 FAIL
        return None


def check_service_runtime_state(ws: Workspace, config: Config) -> CheckResult:
    """IMP-060.01：比对自有服务意图（enabled）与观测（is_running）。

    enabled=true 且未运行 → **FAIL**（当前就是故障），建议文含恢复命令；
    enabled=false → PASS（detail 注明「已按意图停用」）；gateway 在
    ``staticGateway != caddy`` 时不参与判定。
    """
    from local_webpage_access.service_intent import (
        INTENT_ENABLED,
        INTENT_NOT_APPLICABLE,
        SERVICE_NAMES,
        service_intent,
    )

    intent = service_intent(ws, config)
    not_running: list[str] = []
    unknown: list[str] = []
    residual: list[str] = []
    lines: list[str] = []
    for name in SERVICE_NAMES:
        it = intent.get(name)
        if it == INTENT_NOT_APPLICABLE:
            # n.a.（builtin 下的 gateway）无对应 caddy 服务可观测，不参与判定
            lines.append(f"{name}: 不适用（staticGateway={config.staticGateway}）")
            continue
        running = _service_observed_running(name, ws, config)
        if it != INTENT_ENABLED:
            # CHK-224#3：反向不一致——已停用但进程仍在（残留/stale 状态文件）
            # 至少 WARN，否则 doctor 会把残留服务报成健康。
            if running:
                residual.append(name)
                lines.append(f"{name}: 已停用但进程仍在运行（残留）")
            elif running is None:
                lines.append(f"{name}: 已按意图停用（运行态探测失败按未知）")
            else:
                lines.append(f"{name}: 已按意图停用")
            continue
        if running is None:
            unknown.append(name)
            lines.append(f"{name}: enabled，运行态探测失败（未知）")
        elif running:
            lines.append(f"{name}: enabled 且运行中")
        else:
            not_running.append(name)
            lines.append(f"{name}: enabled 但未运行")

    if not_running:
        fixes = "；".join(f"{_SERVICE_START_CMD[n]}（恢复 {n}）" for n in not_running)
        return CheckResult(
            "service_runtime_state",
            STATUS_FAIL,
            f"自有服务运行态与期望不一致：{', '.join(not_running)} enabled 但未运行",
            detail="\n".join(lines),
            suggestion=fixes + "；服务若常随重启/崩溃丢失，请 `lwa autostart check` 检查监管",
        )
    if residual:
        return CheckResult(
            "service_runtime_state",
            STATUS_WARN,
            f"已停用的服务仍有残留进程：{', '.join(residual)}",
            detail="\n".join(lines),
            suggestion="；".join(f"lwa {n} off（清理 {n} 残留进程）" for n in residual),
        )
    message = "自有服务运行态与期望态一致"
    if unknown:
        message += f"（{', '.join(unknown)} 探测失败按未知跳过）"
    return CheckResult(
        "service_runtime_state",
        STATUS_OK,
        message,
        detail="\n".join(lines),
    )


def _container_restart_policy_mismatch() -> tuple[list[str], str | None]:
    """运行中的 ``lwa-<id>`` 容器 restart 策略与模板期望 ``unless-stopped`` 对照。

    返回 ``(不匹配的容器名列表, 错误信息)``；docker 不可用时返回 ``([], None)``——
    韧性子项不因 docker 缺席而告警（check_docker 已负责报 docker 本身）。
    """
    try:
        res = _default_runner(
            ["docker", "ps", "--no-trunc", "--format", "{{.Names}}"],
            capture_output=True,
        )
    except Exception:  # noqa: BLE001
        return [], None
    if res.returncode != 0:
        return [], None
    names = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip().startswith("lwa-")]
    mismatched: list[str] = []
    for name in names:
        try:
            probe = _default_runner(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.HostConfig.RestartPolicy.Name}}",
                    name,
                ],
                capture_output=True,
            )
        except Exception:  # noqa: BLE001
            continue
        if probe.returncode != 0:
            continue
        policy = (probe.stdout or "").strip()
        if policy and policy != "unless-stopped":
            mismatched.append(f"{name}（{policy}）")
    return mismatched, None


def check_restart_resilience(ws: Workspace, config: Config) -> CheckResult:
    """IMP-060.02：评估「机器重启后自有服务能否自动恢复」（WARN 级）。

    四类场景：

    1. 存在 enabled 服务但未装任何自启单元 → WARN（本次事故形态）；
    2. 任一 enabled 服务缺自启单元（逐项差集，CHK-224#1）→ WARN；
    3. Linux/WSL 已装单元但未 linger → WARN（复用 ``autostart.linger_enabled``）；
    4. 运行中容器 restart policy ≠ ``unless-stopped`` → WARN 列实例 ID。

    裸进程是合法模式（开发/临时用途），只 WARN 不 FAIL；韧性检查不做网络探测
    （只读本地文件与 systemctl/launchctl/docker 本地查询）。

    CHK-225 高③④：本检查**不接收注入 runner**——doctor 侧注入 runner 约定为
    单参数签名，而 autostart 后端 / linger / docker 探测需要 kwargs
    （``capture_output`` 等）；透传会让 TypeError 被兜底 except 吞掉，产出
    死代码或恒定 WARN。内部探测一律用各模块自带 kwargs runner；测试侧以
    monkeypatch（``asm.linger_enabled`` 等）注入。
    """
    from local_webpage_access import autostart as asm
    from local_webpage_access.service_intent import (
        INTENT_ENABLED,
        SERVICE_NAMES,
        service_intent,
    )

    try:
        plat = asm.detect_platform()
        backend = asm.select_backend()
    except Exception:  # noqa: BLE001 — 平台不支持自启动时韧性判定不适用
        return CheckResult(
            "restart_resilience",
            STATUS_SKIP,
            "当前平台不支持自启动，跳过重启韧性检查",
        )

    intent = service_intent(ws, config)
    enabled_services = [n for n in SERVICE_NAMES if intent.get(n) == INTENT_ENABLED]
    installed = asm.installed_services(ws, backend)

    warns: list[str] = []
    fixes: list[str] = []
    details: list[str] = [
        f"平台：{plat}；enabled 服务：{', '.join(enabled_services) or '（无）'}；"
        f"已装单元：{', '.join(installed) or '（无）'}",
    ]

    install_cmd = (
        _RECOMMENDED_INSTALL_CMD_LINUX
        if plat in (asm.PLATFORM_LINUX, asm.PLATFORM_WSL)
        else _RECOMMENDED_INSTALL_CMD_MACOS
    )

    # BUG-533：单元文件存在 ≠ 自启生效。实证单元是否被服务管理器
    # enabled（systemctl --user is-enabled / launchctl 非 disabled）；
    # 文件在但 disabled 时重启后同样不会自动拉起。
    disabled_units: list[str] = []
    for name in installed:
        try:
            if not backend.is_enabled(name, asm._default_runner):
                disabled_units.append(name)
        except Exception:  # noqa: BLE001 — 查询失败不误判，仅记录
            log.debug("restart_resilience：查询单元 %s enabled 状态失败", name)
    if disabled_units:
        warns.append(
            f"自启单元已安装但未被服务管理器启用：{'、'.join(disabled_units)}，重启后不会自动拉起"
        )
        fixes.append("lwa autostart enable")

    # CHK-224#1：对 enabled_services 与 installed_services 做**逐项差集**——
    # 只看"一个都没装"或"只缺 gateway"会漏掉部分安装（如 daemon+manager
    # enabled 但只装了 daemon 单元），误报韧性完备。
    missing_units = [n for n in enabled_services if n not in installed]
    if enabled_services and not installed:
        # 场景 1（本次事故形态）：enabled 服务全是裸进程，重启后无人拉起
        warns.append(
            f"{'、'.join(enabled_services)} 处于 enabled 但未安装任何自启动单元，"
            "机器重启后不会自动恢复"
        )
        fixes.append(install_cmd)
    elif missing_units:
        # 场景 2（泛化）：任一 enabled 服务缺自启单元（部分安装）
        gateway_note = "，重启后别名入口会失效" if "gateway" in missing_units else ""
        warns.append(
            f"enabled 服务缺自启单元：{'、'.join(missing_units)}"
            f"（已装：{', '.join(installed) or '无'}）"
            f"{gateway_note}，对应服务重启后不会自动恢复"
        )
        fixes.append(install_cmd)

    if installed and plat in (asm.PLATFORM_LINUX, asm.PLATFORM_WSL) and not asm.linger_enabled():
        # 场景 3：user 单元未 linger，登出后即停止（复用 autostart.run_check 判定）
        warns.append("未 enable-linger，登出后 systemd user 单元会全部停止")
        fixes.append("sudo loginctl enable-linger $USER")

    mismatched, _err = _container_restart_policy_mismatch()
    if mismatched:
        # 场景 4：模板期望 unless-stopped（compose.py），不符的实例重启后不自愈
        warns.append(
            "运行中容器的 restart 策略与模板期望 unless-stopped 不符：" + "、".join(mismatched)
        )
        fixes.append("lwa rebuild <id> 重新生成 compose（或人工核对 restart 策略）")

    if warns:
        return CheckResult(
            "restart_resilience",
            STATUS_WARN,
            "重启韧性有缺口（重启/登出后部分服务不会自动恢复）",
            detail="\n".join([*details, *[f"⚠️ {w}" for w in warns]]),
            suggestion="；".join(dict.fromkeys(fixes)),
        )
    return CheckResult(
        "restart_resilience",
        STATUS_OK,
        "重启韧性完备（自启单元 / linger / 容器 restart 策略均符合期望）",
        detail="\n".join(details),
    )


# ---- 聚合入口（WBS-26.01/11）-----------------------------------------------


def run_doctor(
    ws: Workspace,
    config: Config,
    *,
    instance_id: str | None = None,
    access_review: bool = False,
    runner: SubprocessRunner = _default_runner,
    port_in_use: PortChecker = _default_port_in_use,
) -> DoctorReport:
    """运行全部环境检查；若提供 instance_id 则附加实例诊断。

    ``access_review=True``（``lwa doctor --access``）时复用
    :func:`local_webpage_access.access.review_access`，不重写探测逻辑。
    """
    report = DoctorReport()
    allocated_ports = _allocated_ports_for_workspace(ws)
    # IMP-020：打开一个 registry 供 caddy 健康探针探测站点/别名入口可达性；
    # 打开失败不阻断整体诊断（check_caddy_health 内部对 None registry 安全降级）。
    caddy_probe_registry: Registry | None = None
    try:
        caddy_probe_registry = Registry(ws.db_path)
        caddy_probe_registry.open_readonly()
    except Exception:  # noqa: BLE001
        caddy_probe_registry = None
    try:
        from local_webpage_access.ports import resolve_lan_ip

        report.current_lan_ip = resolve_lan_ip(config)
        if caddy_probe_registry is not None:
            drifted_pairs, _skipped = _collect_lan_drifted_ids(
                ws, caddy_probe_registry, report.current_lan_ip
            )
            report.drifted_instance_ids = [iid for iid, _host in drifted_pairs]

        report.checks = [
            check_python_version(),
            check_python_packages(),
            check_docker(runner=runner),
            check_docker_compose(runner=runner),
            # issue #14：基础镜像就绪度（纯本地，WARN 级）
            check_base_image_readiness(ws, runner=runner),
            check_caddy(config, runner=runner),
            # IMP-065：git 可执行（缺失仅 WARN，不影响 zip/文件夹导入）
            check_git(runner=runner),
            check_port_pool(config, port_in_use=port_in_use, allocated_ports=allocated_ports),
            check_registry(ws),
            check_static_gateway(ws),
            # IMP-060：自有服务运行态（FAIL 级）+ 重启韧性（WARN 级）
            check_service_runtime_state(ws, config),
            check_restart_resilience(ws, config),
            check_caddy_health(ws, config, runner=runner, registry=caddy_probe_registry),
            check_lan_url_stale(ws, config, caddy_probe_registry)
            if caddy_probe_registry is not None
            else CheckResult("lan_url_stale", STATUS_SKIP, "registry 不可用，跳过 lanUrl 漂移检测"),
            check_workspace_path_consistency(ws, config, registry=caddy_probe_registry),
            # issue #8：源码新鲜度（WARN 级，纯离线；git 源不触网）
            check_source_freshness(ws),
            check_backend_handoff(ws, config, caddy_probe_registry)
            if caddy_probe_registry is not None
            else CheckResult("backend_handoff", STATUS_SKIP, "registry 不可用，跳过后端交接检测"),
            check_port_contention(ws, config, registry=caddy_probe_registry),
            check_disk_space(ws),
            check_memory(),
        ]
        if access_review and caddy_probe_registry is not None:
            from local_webpage_access.access_workflow import review_access

            try:
                report.access_review = review_access(ws, config, caddy_probe_registry)
            except Exception as exc:  # noqa: BLE001
                report.checks.append(
                    CheckResult(
                        "access_review",
                        STATUS_FAIL,
                        f"访问复核失败：{exc}",
                        suggestion="手动运行 `lwa access review`",
                    )
                )
    finally:
        if caddy_probe_registry is not None:
            with contextlib.suppress(Exception):
                caddy_probe_registry.close()
    if instance_id:
        report.instance_id = instance_id
        if not ws.db_path.is_file():
            # 评审-组5：reg.open() 缺库时会创建空 DB，违背诊断只读承诺
            report.instance_checks = [
                CheckResult(
                    f"instance:{instance_id}",
                    STATUS_FAIL,
                    f"registry 不存在：{ws.db_path}（实例诊断不创建数据库）",
                )
            ]
            return report
        try:
            reg = Registry(ws.db_path)
            reg.open_readonly()
            try:
                report.instance_checks = diagnose_instance(ws, reg, instance_id)
            finally:
                reg.close()
        except Exception as exc:
            report.instance_checks = [
                CheckResult(
                    f"instance:{instance_id}",
                    STATUS_FAIL,
                    f"实例诊断失败：{exc}",
                )
            ]
    return report


def _allocated_ports_for_workspace(ws: Workspace) -> set[int]:
    if not ws.db_path.is_file():
        return set()
    try:
        reg = Registry(ws.db_path)
        reg.open()
        try:
            return set(reg.allocated_ports())
        finally:
            reg.close()
    except Exception:
        return set()


def format_report(report: DoctorReport) -> str:
    """把报告渲染成人类可读文本（供 CLI 输出）。"""
    lines: list[str] = []
    lines.append("── 环境检查 ──")
    for c in report.checks:
        lines.append(f"  [{c.status.upper():4}] {c.name}: {c.message}")
        if c.detail:
            lines.append(f"           详情：{c.detail}")
        if c.suggestion:
            lines.append(f"           建议：{c.suggestion}")
    if report.instance_id:
        lines.append("")
        lines.append(f"── 实例诊断：{report.instance_id} ──")
        for c in report.instance_checks:
            lines.append(f"  [{c.status.upper():4}] {c.message}")
            if c.detail:
                lines.append(f"           详情：{c.detail}")
            if c.suggestion:
                lines.append(f"           建议：{c.suggestion}")
    lines.append("")
    n_fail = len([c for c in report.checks + report.instance_checks if c.status == STATUS_FAIL])
    n_warn = len([c for c in report.checks + report.instance_checks if c.status == STATUS_WARN])
    summary = f"总体：{report.overall.upper()}（{n_fail} 失败，{n_warn} 警告）"
    lines.append(summary)
    return "\n".join(lines)


__all__ = [
    "STATUS_OK",
    "STATUS_WARN",
    "STATUS_FAIL",
    "STATUS_SKIP",
    "CheckResult",
    "DoctorReport",
    "SubprocessRunner",
    "PortChecker",
    "check_python_version",
    "check_python_packages",
    "check_docker",
    "check_docker_compose",
    "check_caddy",
    "check_git",
    "check_port_pool",
    "check_registry",
    "check_static_gateway",
    "check_service_runtime_state",
    "check_restart_resilience",
    "check_caddy_health",
    "check_workspace_path_consistency",
    "check_lan_url_stale",
    "check_source_freshness",
    "check_backend_handoff",
    "check_base_image_readiness",
    "check_port_contention",
    "check_disk_space",
    "check_memory",
    "diagnose_instance",
    "run_doctor",
    "format_report",
]
