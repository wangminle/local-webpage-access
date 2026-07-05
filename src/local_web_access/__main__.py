"""允许通过 `python -m local_web_access` 运行 CLI。"""

from local_web_access.cli import app

if __name__ == "__main__":
    app()
