"""容器运行身份解析（issue #20）。

生成的容器默认以 root 运行（无 ``USER`` 指令），而 ``/app/data`` 是宿主
bind mount——运行时属主由宿主目录决定，镜像构建期的 ``chown`` 会被覆盖。
故采用**宿主数据目录 UID/GID 对齐**方案：

1. 目标身份统一取实例 ``apps/<id>/data/`` 目录的宿主属主（uid/gid）；
2. Dockerfile 末尾生成 ``USER <uid>:<gid>``（Docker 支持数字形式，不要求
   镜像内存在同名用户），并以 ``ENV HOME=/tmp`` 提供可写 HOME；
3. Compose 增加 ``user: "<uid>:<gid>"`` 防御层，覆盖镜像默认用户，防模板
   漂移回 root；
4. Dockerfile 与 Compose 必须调用本模块的同一解析结果，禁止各自推导。

``ContainerConfig.runAsNonRoot`` 三态语义：

- ``True``：非 root 运行（新导入实例默认）；
- ``None``：旧 manifest 缺失，legacy root 运行并告警，待显式迁移；
- ``False``：用户显式选择 root 兼容。
"""

from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass

from local_webpage_access.errors import ConfigError
from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest
from local_webpage_access.paths import Workspace

log = get_logger("container_identity")

__all__ = [
    "ContainerIdentity",
    "resolve_container_identity",
    "ensure_non_root_identity_ready",
]


@dataclass(frozen=True)
class ContainerIdentity:
    """容器应使用的运行身份（宿主 data/ 属主对齐）。"""

    uid: int
    gid: int

    def docker_user(self) -> str:
        """Dockerfile ``USER`` / Compose ``user:`` 使用的 ``uid:gid`` 字面值。"""
        return f"{self.uid}:{self.gid}"


def resolve_container_identity(
    manifest: InstanceManifest,
    workspace: Workspace,
) -> ContainerIdentity | None:
    """解析容器运行身份；未启用非 root 时返回 ``None``。

    读取实例 ``data/`` 目录的宿主 UID/GID（LWA 导入时创建该目录）。目录
    尚不存在时（理论上只在异常现场）以当前进程身份兜底并顺手建目录，
    保证 Dockerfile 与 Compose 两侧拿到同一个值。
    """
    container = manifest.container
    if container is None:
        return None
    if container.runAsNonRoot is not True:
        return None

    data_dir = workspace.app_data(manifest.id)
    try:
        st = data_dir.stat()
    except OSError:
        data_dir.mkdir(parents=True, exist_ok=True)
        st = data_dir.stat()
    return ContainerIdentity(uid=st.st_uid, gid=st.st_gid)


def ensure_non_root_identity_ready(
    manifest: InstanceManifest,
    workspace: Workspace,
) -> ContainerIdentity | None:
    """rebuild 前预检容器运行身份（在停止旧容器**之前**调用）。

    - ``runAsNonRoot=True`` 且 ``data/`` 属主为 root（uid=0）：直接拒绝并
      提示先修正属主——此时停机再失败只会留下已停且无法恢复的现场；
    - ``runAsNonRoot=True`` 且属主可写：放行，返回解析出的身份；
    - ``runAsNonRoot=None``（旧实例）：告警后放行（legacy root，待显式迁移）；
    - ``runAsNonRoot=False``：INFO 留痕后放行（用户显式选择）。

    非 root 容器写 SQLite 还要求属主对目录有写权限（创建 -wal/-shm），
    属主无写权限同样拒绝。
    """
    container = manifest.container
    if container is None:
        return None

    identity = resolve_container_identity(manifest, workspace)
    if identity is None:
        if container.runAsNonRoot is None:
            log.warning(
                "实例 %s：manifest 缺少 container.runAsNonRoot，按 legacy root 运行"
                "（可用 lwa migrate-user %s 显式迁移到非 root）",
                manifest.id,
                manifest.id,
            )
        else:
            log.info(
                "实例 %s：runAsNonRoot=False（用户显式选择），容器以 root 运行",
                manifest.id,
            )
        return None

    data_dir = workspace.app_data(manifest.id)
    if identity.uid == 0:
        raise ConfigError(
            f"实例 {manifest.id} 启用了非 root 运行（runAsNonRoot=True），但宿主数据目录 "
            f"{data_dir} 属主为 root(0)：bind mount 场景下该目录运行时无法被非 root "
            f"容器写入（SQLite 会失败）。请先把属主改为普通用户，例如：\n"
            f"  sudo chown -R <uid>:<gid> {data_dir}\n"
            f"改完重试；确需 root 兼容可在 local-web.json 设置 "
            f'container.runAsNonRoot=false。',
            instance_id=manifest.id,
        )
    try:
        mode = stat_module.S_IMODE(data_dir.stat().st_mode)
    except OSError as exc:  # pragma: no cover - resolve 刚 stat 过，极端竞态
        raise ConfigError(
            f"实例 {manifest.id} 无法读取数据目录 {data_dir} 权限：{exc}",
            instance_id=manifest.id,
        ) from exc
    if not mode & stat_module.S_IWUSR:
        raise ConfigError(
            f"实例 {manifest.id} 的数据目录 {data_dir} 属主（uid={identity.uid}）"
            f"无写权限（mode={oct(mode)}），非 root 容器无法写入 SQLite。"
            f"请修正：chmod u+w {data_dir}",
            instance_id=manifest.id,
        )
    log.info(
        "实例 %s：容器将以非 root 身份 %s 运行（对齐宿主 data/ 属主）",
        manifest.id,
        identity.docker_user(),
    )
    return identity


def current_host_identity() -> ContainerIdentity:
    """当前进程的宿主身份（迁移命令展示用）。"""
    return ContainerIdentity(uid=os.getuid(), gid=os.getgid())
