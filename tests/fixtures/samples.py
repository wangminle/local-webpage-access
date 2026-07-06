"""V1 验收样例与测试夹具（WBS-27）。

每个样例以 ``{相对路径: 内容}`` 字典定义源文件树，由 :func:`build_zip`
在测试中按需打包成 zip（避免在仓库中提交二进制 zip，便于跨平台与维护）。

样例清单（对应 WBS-27.01~08）：

* ``static_html`` —— 纯静态 HTML（WBS-27.01），识别为 ``static``。
* ``vite_react`` —— Vite + React 纯前端（WBS-27.02），识别为 ``node`` 前端。
* ``node_express`` —— Node/Express 后端无 DB（WBS-27.03），识别为 ``node`` 后端。
* ``fastapi_sqlite`` —— FastAPI + SQLite（WBS-27.04），识别为 ``python``。
* ``build_failure`` —— 构建会失败的 Node 项目（WBS-27.05），用于触发 ``failed``。
* ``pending_unknown`` —— 无法识别（WBS-27.06），保持 ``pending``。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

# ---- 样例源文件树 ---------------------------------------------------------

STATIC_HTML = {
    "index.html": (
        "<!DOCTYPE html>\n"
        "<html lang=\"zh\">\n"
        "<head><meta charset=\"utf-8\"><title>Static Demo</title>\n"
        "<link rel=\"stylesheet\" href=\"css/style.css\"></head>\n"
        "<body><h1>Hello from lwa static demo</h1>\n"
        "<p>这是一个纯静态 HTML 样例。</p>\n"
        "<script src=\"js/app.js\"></script></body></html>\n"
    ),
    "css/style.css": "body { font-family: sans-serif; margin: 2rem; }\nh1 { color: #2a7; }\n",
    "js/app.js": "console.log('static demo loaded');\n",
    "README.md": "# static-html\n\n纯静态 HTML 样例，用于验证 lwa 静态托管路径。\n",
}

VITE_REACT = {
    "package.json": (
        "{\n"
        '  "name": "vite-react-demo",\n'
        '  "private": true,\n'
        '  "version": "1.0.0",\n'
        '  "type": "module",\n'
        '  "scripts": {\n'
        '    "dev": "vite",\n'
        '    "build": "vite build",\n'
        '    "preview": "vite preview --port 4173"\n'
        "  },\n"
        '  "dependencies": {\n'
        '    "react": "^18.2.0",\n'
        '    "react-dom": "^18.2.0"\n'
        "  },\n"
        '  "devDependencies": {\n'
        '    "@vitejs/plugin-react": "^4.2.0",\n'
        '    "vite": "^5.0.0"\n'
        "  }\n"
        "}\n"
    ),
    "vite.config.js": (
        "import { defineConfig } from 'vite'\n"
        "import react from '@vitejs/plugin-react'\n"
        "export default defineConfig({ plugins: [react()] })\n"
    ),
    "index.html": (
        "<!DOCTYPE html>\n"
        '<html><head><meta charset="utf-8"><title>Vite React</title></head>\n'
        '<body><div id="root"></div>\n'
        '<script type="module" src="/src/main.jsx"></script></body></html>\n'
    ),
    "src/main.jsx": (
        "import React from 'react'\n"
        "import { createRoot } from 'react-dom/client'\n"
        "import App from './App.jsx'\n"
        "createRoot(document.getElementById('root')).render(<App />)\n"
    ),
    "src/App.jsx": (
        "import React from 'react'\n"
        "export default function App() {\n"
        "  return <h1>Hello from Vite + React</h1>\n"
        "}\n"
    ),
}

NODE_EXPRESS = {
    "package.json": (
        "{\n"
        '  "name": "node-express-demo",\n'
        '  "version": "1.0.0",\n'
        '  "type": "module",\n'
        '  "scripts": {\n'
        '    "start": "node server.js"\n'
        "  },\n"
        '  "dependencies": {\n'
        '    "express": "^4.19.0"\n'
        "  }\n"
        "}\n"
    ),
    "server.js": (
        "import express from 'express'\n"
        "const app = express()\n"
        "const PORT = process.env.PORT || 3000\n"
        "app.get('/', (req, res) => {\n"
        "  res.json({ ok: true, app: 'node-express-demo' })\n"
        "})\n"
        "app.get('/health', (req, res) => res.json({ status: 'ok' }))\n"
        "app.listen(PORT, () => console.log(`listening on ${PORT}`))\n"
    ),
    "README.md": "# node-express-demo\n\nNode/Express 后端样例，无数据库。\n",
}

FASTAPI_SQLITE = {
    "requirements.txt": "fastapi==0.115.0\nuvicorn==0.30.0\n",
    "main.py": (
        "import os, sqlite3\n"
        "from fastapi import FastAPI\n"
        "app = FastAPI(title='fastapi-sqlite-demo')\n"
        "DB = os.environ.get('DATABASE_URL', 'sqlite:///app.db')\n"
        "@app.get('/')\n"
        "def index():\n"
        "    return {'ok': True, 'db': DB}\n"
        "@app.get('/health')\n"
        "def health():\n"
        "    return {'status': 'ok'}\n"
    ),
    "README.md": "# fastapi-sqlite-demo\n\nFastAPI + SQLite 样例。\n",
}

BUILD_FAILURE = {
    "package.json": (
        "{\n"
        '  "name": "build-failure-demo",\n'
        '  "version": "1.0.0",\n'
        '  "type": "module",\n'
        '  "scripts": {\n'
        '    "start": "node server.js"\n'
        "  },\n"
        '  "dependencies": {\n'
        '    "express": "^4.19.0"\n'
        "  }\n"
        "}\n"
    ),
    # 故意引用不存在的入口文件，使启动失败 → 实例进入 failed
    "server.js": (
        "import express from 'express'\n"
        "import './nonexistent-module.js'  // 故意制造启动失败\n"
        "const app = express()\n"
        "app.listen(process.env.PORT || 3000)\n"
    ),
    "README.md": "# build-failure-demo\n\n故意制造启动失败的样例。\n",
}

PENDING_UNKNOWN = {
    "readme.txt": (
        "这是一个没有任何可识别技术栈标记的目录。\n"
        "没有 index.html / package.json / requirements.txt / pyproject.toml。\n"
        "lwa 应把它识别为 pending，不会自动构建或启动。\n"
    ),
    "notes.md": "# notes\n\n只是一些文本文件。\n",
}

# 样例注册表
SAMPLES: dict[str, dict[str, str]] = {
    "static_html": STATIC_HTML,
    "vite_react": VITE_REACT,
    "node_express": NODE_EXPRESS,
    "fastapi_sqlite": FASTAPI_SQLITE,
    "build_failure": BUILD_FAILURE,
    "pending_unknown": PENDING_UNKNOWN,
}

# 每个样例的预期识别结果（kind），供验收测试断言
EXPECTED_KIND = {
    "static_html": "static",
    "vite_react": "node",
    "node_express": "node",
    "fastapi_sqlite": "python",
    "build_failure": "node",
    "pending_unknown": None,  # pending
}


# ---- 构建器 ----------------------------------------------------------------


def build_zip(name: str, dest: Path) -> Path:
    """把样例 ``name`` 打包成 zip 写到 ``dest``，返回 zip 路径。

    Args:
        name: :data:`SAMPLES` 中的样例名。
        dest: 目标 zip 路径（或目录；若为目录则用 ``<name>.zip``）。

    Raises:
        KeyError: 未知样例名。
    """
    if name not in SAMPLES:
        raise KeyError(f"未知样例：{name}（可选：{sorted(SAMPLES)}）")
    files = SAMPLES[name]
    dest = Path(dest)
    if dest.is_dir():
        dest = dest / f"{name}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, content in files.items():
            zf.writestr(rel, content)
    return dest


def build_all(dest_dir: Path) -> dict[str, Path]:
    """把全部样例打包到 ``dest_dir``，返回 {name: zip_path}。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    return {name: build_zip(name, dest_dir / f"{name}.zip") for name in SAMPLES}


__all__ = [
    "STATIC_HTML",
    "VITE_REACT",
    "NODE_EXPRESS",
    "FASTAPI_SQLITE",
    "BUILD_FAILURE",
    "PENDING_UNKNOWN",
    "SAMPLES",
    "EXPECTED_KIND",
    "build_zip",
    "build_all",
]
