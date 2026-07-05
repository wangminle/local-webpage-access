"""SQLite Registry 子包。

提供连接管理、schema 迁移和实例/容器/静态站点/端口/事件/构建/资源的数据访问。
对应 WBS-05 与 V1 设计说明第 9 节。
"""

from local_web_access.registry.dao import Registry

__all__ = ["Registry"]
