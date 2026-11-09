"""工作区的 LAN 地址策略，可在后台服务运行期间更新。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode

from local_webpage_access.atomic_io import atomic_write_text
from local_webpage_access.config import Config, load_config
from local_webpage_access.config_edit import config_save_lock
from local_webpage_access.errors import ConfigError
from local_webpage_access.paths import Workspace


@lru_cache(maxsize=64)
def _read_policy(root: Path, _stamp: tuple[int, int, int]) -> tuple[str, str | None]:
    config = load_config(Workspace(root))
    return config.lanIpStrategy, config.manualLanIp


def config_with_latest_lan_policy(workspace: Workspace, config: Config) -> Config:
    """只更新 LAN 两个配置字段，保留调用方的其余设置。"""
    try:
        stat = workspace.config_path.stat()
    except OSError:
        return config
    try:
        mode, address = _read_policy(workspace.root, (stat.st_ino, stat.st_mtime_ns, stat.st_size))
    except ConfigError:
        # 运行中的服务可继续使用上次的配置。CLI 启动时仍会报告无效配置。
        return config
    if (mode, address) == (config.lanIpStrategy, config.manualLanIp):
        return config
    return config.model_copy(update={"lanIpStrategy": mode, "manualLanIp": address})


def _updated_yaml(text: str, values: dict[str, str | None]) -> str:
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if root is None:
        return text.rstrip() + "\n" + yaml.safe_dump(values, sort_keys=False)
    if not isinstance(root, MappingNode):
        raise ConfigError("LAN 策略保存失败：配置顶层须为 YAML 映射")

    missing = values.copy()
    edits: list[tuple[int, int, str]] = []
    for key, value in root.value:
        if not isinstance(key, ScalarNode) or key.value not in values:
            continue
        if key.value not in missing:
            raise ConfigError(f"LAN 策略保存失败：配置项 {key.value} 重复")
        if not isinstance(value, ScalarNode) or value.start_mark.index < key.end_mark.index:
            raise ConfigError(f"LAN 策略保存失败：配置项 {key.value} 须为独立标量")
        start = value.start_mark.index
        end = value.end_mark.index
        replacement = json.dumps(missing.pop(key.value))
        if text[start:end].endswith("\n"):
            replacement += "\n"
        if start == end and start and text[start - 1] == ":":
            replacement = " " + replacement
        edits.append((start, end, replacement))
    if missing:
        end = root.end_mark.index
        if root.flow_style:
            prefix = ", " if root.value and not text[:end - 1].rstrip().endswith(",") else ""
            entries = ", ".join(f"{key}: {json.dumps(value)}" for key, value in missing.items())
            edits.append((end - 1, end - 1, prefix + entries))
        else:
            prefix = "\n" if end and text[end - 1] != "\n" else ""
            edits.append((end, end, prefix + yaml.safe_dump(missing, sort_keys=False)))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def write_lan_policy(workspace: Workspace, address: str | None) -> Config:
    """只写入 LAN 策略字段，锁内校验完整配置并原子替换。"""
    path = workspace.config_path
    mode = "auto" if address is None else "manual"
    with config_save_lock(path):
        try:
            text = path.read_text(encoding="utf-8")
            updated = _updated_yaml(text, {"lanIpStrategy": mode, "manualLanIp": address})
            data = yaml.safe_load(updated)
            # 与 load_config 的历史配置兼容逻辑保持一致。
            if data.get("staticGateway") == "nginx":
                data["staticGateway"] = "builtin"
            config = Config.from_dict(data)
            if (config.lanIpStrategy, config.manualLanIp) != (mode, address):
                raise ConfigError("LAN 策略保存失败：保存值被其他配置项覆盖")
            atomic_write_text(path, updated)
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"LAN 策略保存失败：{exc}") from exc
    return config
