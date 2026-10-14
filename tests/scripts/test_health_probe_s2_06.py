"""S2-06：健康端点路由前缀合成（#50 后半 / BUG-742）。

- FastAPI：同文件 ``APIRouter(prefix=...)`` 第 1 层；入口文件
  ``include_router(<module>.router, prefix=...)`` 第 2 层（模块跨文件解析）；
- Node：``app.use('<prefix>', <router>)`` 同文件挂载与 ``require('./x')``
  跨文件挂载；
- 无法合成完整路径的探针以路由原文入探针、标 ``confidence="low"``，
  description 与运行时 probeNotes 都留痕；S1-04 内容校验兜底，
  拼错只会变成「返回 HTML」提示，不构成失败（不造成假绿）。

test_50_router_prefix_composed_and_deep_route_walked 为
design/5-checkpoints/2026-10-11-issues47-60-repro.py::test_50 的前缀合成
翻转迁移（S1-04 已翻转深度/命名部分）。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from local_webpage_access import hosting
from local_webpage_access.candidate_generator import (
    DeploymentCandidate,
    _discover_health_probes,
)
from local_webpage_access.models import ProjectEvidence

from tests.scripts.test_probe_semantics_batch2 import _manifest


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _discover(tmp_path: Path, *, kind: str = "python", source_subdir: str | None = None):
    backend = DeploymentCandidate(
        kind=kind,
        runtime="docker_compose",
        servingMode="container",
        form="backend-container",
        sourceSubdir=source_subdir,
    )
    return _discover_health_probes(ProjectEvidence(root=str(tmp_path)), backend)


# ---- FastAPI：APIRouter 第 1 层（翻转 repro.py::test_50 的前缀合成断言）-------


def test_50_router_prefix_composed_and_deep_route_walked(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "backend/app/health.py",
        'router = APIRouter(prefix="/api/v1")\n@router.get("/health")\ndef f(): pass',
    )
    _write(
        tmp_path,
        "backend/app/api/v1/health.py",
        '@router.get("/ready")\ndef g(): pass',
    )
    probes = _discover(tmp_path, source_subdir="backend")
    by_path = {p.path: p for p in probes}
    # APIRouter(prefix=...) 第 1 层合成 /api/v1/health；但扫描到的文件中
    # 没有 include_router 挂载点，挂载前缀无法证实 → 低置信度 + 说明
    assert by_path["/api/v1/health"].confidence == "low"
    assert "include_router" in by_path["/api/v1/health"].description
    # 深度 6（S1-04）可达深层文件；router 未解析到挂载点 → 低置信度 + 说明
    assert by_path["/ready"].confidence == "low"
    assert "挂载前缀" in by_path["/ready"].description

    # 翻转原断言：public-health 命名（S1-04）即便前缀未知也不再被漏掉，
    # 以路由原文低置信度入探针。
    (tmp_path / "backend" / "app" / "health.py").write_text(
        '@router.get("/public-health")\ndef f(): pass', encoding="utf-8"
    )
    probes2 = _discover(tmp_path, source_subdir="backend")
    public_health = {p.path: p for p in probes2}["/public-health"]
    assert public_health.confidence == "low"
    assert public_health.source == "discovered"
    assert public_health.isMandatory is False


def test_api_router_prefix_with_local_include_router_stack(tmp_path: Path) -> None:
    """两层都在同一文件：include_router(router, prefix=...) 叠加 APIRouter 前缀。"""
    _write(
        tmp_path,
        "main.py",
        "from fastapi import FastAPI, APIRouter\n"
        "app = FastAPI()\n"
        'router = APIRouter(prefix="/v1")\n'
        'app.include_router(router, prefix="/api")\n'
        '@router.get("/health")\ndef h(): pass',
    )
    probes = _discover(tmp_path)
    assert [(p.path, p.confidence) for p in probes] == [("/api/v1/health", "high")]


# ---- FastAPI：include_router 模块跨文件（home-bookshelf 布局）-----------------


def test_include_router_cross_module_prefix_two_layers(tmp_path: Path) -> None:
    """home-bookshelf 验收形态：api/v1/health.py 的路由与入口前缀拼成
    /api/v1/public-health，不再依赖手工 ``lwa probe set``。"""
    _write(
        tmp_path,
        "backend/app/main.py",
        "from fastapi import FastAPI\n"
        "from app.api.v1 import health\n"
        "app = FastAPI()\n"
        'app.include_router(health.router, prefix="/api/v1")\n',
    )
    _write(
        tmp_path,
        "backend/app/api/v1/health.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        '@router.get("/public-health")\ndef h():\n    return {}\n',
    )
    _write(
        tmp_path,
        "backend/app/api/v1/books.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        '@router.get("/books")\ndef b():\n    return []\n',  # 非健康命名，不入探针
    )
    probes = _discover(tmp_path, source_subdir="backend")
    assert [(p.path, p.confidence) for p in probes] == [("/api/v1/public-health", "high")]


def test_include_router_module_prefix_combined_with_api_router_prefix(tmp_path: Path) -> None:
    """跨文件模块自身还带 APIRouter 前缀时两层相乘。"""
    _write(
        tmp_path,
        "main.py",
        "from fastapi import FastAPI\n"
        "from api import health\n"
        "app = FastAPI()\n"
        'app.include_router(health.router, prefix="/api")\n',
    )
    _write(
        tmp_path,
        "api/health.py",
        "from fastapi import APIRouter\n"
        'router = APIRouter(prefix="/v1")\n'
        '@router.get("/health")\ndef h(): pass',
    )
    probes = _discover(tmp_path)
    assert [(p.path, p.confidence) for p in probes] == [("/api/v1/health", "high")]


def test_include_router_chain_with_intermediate_api_router_constructor_prefix(
    tmp_path: Path,
) -> None:
    """BUG-786 回归（home-bookshelf 真实形态，39 条路由）：中间层
    ``api_router = APIRouter(prefix="/api/v1")`` 的构造器前缀要沿
    ``api_router.include_router(health.router)`` +
    ``app.include_router(api_router)`` 链累加，合成
    ``/api/v1/public-health``——此前漏掉中间层前缀，错标 high 的
    ``/public-health`` 实测返回 SPA 兜底 HTML。"""
    _write(
        tmp_path,
        "backend/app/main.py",
        "from fastapi import FastAPI\n"
        "from app.api.router import api_router\n"
        "app = FastAPI()\n"
        "app.include_router(api_router)\n",
    )
    _write(
        tmp_path,
        "backend/app/api/router.py",
        "from fastapi import APIRouter\n"
        "from app.api.v1 import health\n"
        'api_router = APIRouter(prefix="/api/v1")\n'
        "api_router.include_router(health.router)\n",
    )
    _write(
        tmp_path,
        "backend/app/api/v1/health.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        '@router.get("/public-health")\ndef h(): pass',
    )
    probes = _discover(tmp_path, source_subdir="backend")
    assert [(p.path, p.confidence) for p in probes] == [("/api/v1/public-health", "high")]


def test_apirouter_with_dependencies_kwarg_keeps_prefix(tmp_path: Path) -> None:
    """BUG-792：``APIRouter(dependencies=[Depends(...)], prefix="/api")`` 的嵌套
    括号不得在首个 ``)`` 截断参数——prefix 在其后时丢失会合成出缺前缀的错误
    路径并误标 high。include_router 同理（dependencies 在 prefix 前）。"""
    _write(
        tmp_path,
        "app/main.py",
        "from fastapi import FastAPI\n"
        "from app.api import api_router\n"
        "app = FastAPI()\n"
        "app.include_router(api_router)\n",
    )
    _write(
        tmp_path,
        "app/api.py",
        "from fastapi import APIRouter, Depends\n"
        "from app import health\n"
        "def _auth(): pass\n"
        'api_router = APIRouter(dependencies=[Depends(_auth)], prefix="/api")\n'
        "api_router.include_router(health.router, dependencies=[Depends(_auth)])\n",
    )
    _write(
        tmp_path,
        "app/health.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        '@router.get("/healthz")\ndef h(): pass',
    )
    probes = _discover(tmp_path)
    assert [(p.path, p.confidence) for p in probes] == [("/api/healthz", "high")]


# ---- Node：app.use 前缀 --------------------------------------------------------


def test_node_app_use_prefix_same_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "server.js",
        "const express = require('express');\n"
        "const app = express();\n"
        "const r = express.Router();\n"
        "app.use('/api/v1', r);\n"
        "r.get('/health', (req, res) => res.json({ok: true}));\n",
    )
    probes = _discover(tmp_path, kind="node")
    assert [(p.path, p.confidence) for p in probes] == [("/api/v1/health", "high")]


def test_node_app_use_prefix_cross_file_require(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "server.js",
        "const express = require('express');\n"
        "const app = express();\n"
        "const healthRouter = require('./routes/health');\n"
        "app.use('/api', healthRouter);\n",
    )
    _write(
        tmp_path,
        "routes/health.js",
        "const express = require('express');\n"
        "const router = express.Router();\n"
        "router.get('/public-health', (req, res) => res.json({ok: true}));\n"
        "module.exports = router;\n",
    )
    probes = _discover(tmp_path, kind="node")
    assert [(p.path, p.confidence) for p in probes] == [("/api/public-health", "high")]


def test_node_chained_use_prefix_two_layers(tmp_path: Path) -> None:
    """同文件链式挂载：app.use('/api', sub) + sub.use('/v1', r) → /api/v1/health。"""
    _write(
        tmp_path,
        "server.js",
        "const express = require('express');\n"
        "const app = express();\n"
        "const sub = express.Router();\n"
        "const r = express.Router();\n"
        "app.use('/api', sub);\n"
        "sub.use('/v1', r);\n"
        "r.get('/health', (req, res) => res.json({ok: true}));\n",
    )
    probes = _discover(tmp_path, kind="node")
    assert [(p.path, p.confidence) for p in probes] == [("/api/v1/health", "high")]


# ---- 合成失败：低置信度 + probeNotes 留痕（不造成假绿）-------------------------


def test_unresolvable_router_var_is_low_confidence(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        '@router.get("/health")\ndef h(): pass',  # router 变量未定义、无处挂载
    )
    probes = _discover(tmp_path)
    assert [(p.path, p.confidence) for p in probes] == [("/health", "low")]
    assert "前缀" in probes[0].description


def test_node_unmounted_router_var_is_low_confidence(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "server.js",
        "const express = require('express');\n"
        "const router = express.Router();\n"
        "router.get('/health', (req, res) => res.json({ok: true}));\n",
    )
    probes = _discover(tmp_path, kind="node")
    assert [(p.path, p.confidence) for p in probes] == [("/health", "low")]


def test_low_confidence_probe_writes_probe_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    """低置信度探针：验证时写 probeNotes，但探针通过时整体仍 passed（只留痕不失败）。"""
    monkeypatch.setattr(hosting, "_wait_for_http", lambda *a, **k: True)
    monkeypatch.setattr(
        hosting,
        "_probe_path",
        lambda *a, **k: (True, 200, "application/json"),
    )
    m = _manifest(
        probes=[
            {
                "path": "/health",
                "isMandatory": False,
                "source": "discovered",
                "confidence": "low",
            },
            {
                "path": "/api/v1/public-health",
                "isMandatory": False,
                "source": "discovered",
                "confidence": "high",
            },
        ]
    )
    result = hosting._evaluate_container_verification(
        19999, m, MagicMock(), MagicMock(), m.id
    )
    assert result["overall_status"] == "passed"
    low_notes = [n for n in result["probe_notes"] if "置信度低" in n]
    assert low_notes == ["/health 由静态路由扫描合成，置信度低（挂载前缀可能不完整）"]
