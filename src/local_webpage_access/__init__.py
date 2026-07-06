"""Local Webpage Access — 面向局域网小主机的本地网页部署基座.

zip 导入 → 项目识别 → 运行形态选择 → 静态托管或 Docker Compose → 统一登记 → 管理页展示。

正式产品名称：Local Webpage Access；CLI 命令：``lwa``；Python 分发包名：``local-webpage-access``。
"""

PRODUCT_NAME = "Local Webpage Access"

from local_webpage_access.version_info import resolve_version

__version__ = resolve_version()

__all__ = ["PRODUCT_NAME", "__version__"]
