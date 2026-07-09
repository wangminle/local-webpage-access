"""支持 ``python3 -m local_webpage_access.cli`` 直接执行（BUG-093）。

DEV-044 将单文件 ``cli.py`` 拆成 ``cli/`` 包后，``-m local_webpage_access.cli``
会寻找包内的 ``__main__.py``；``__init__.py`` 里的 ``if __name__ == "__main__"``
guard 在以包形式执行时不被触发，故补此入口委托到 :func:`local_webpage_access.cli.run`。
"""

from __future__ import annotations

from local_webpage_access.cli import run

if __name__ == "__main__":
    run()
