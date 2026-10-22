"""BUG-952 / issue #142：导入期别名守卫须复用构建型前端豁免，不得把源码入口当产物拦下。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from local_webpage_access.errors import RecognitionError
from local_webpage_access.importer import Importer
from local_webpage_access.models import Runtime

_SOURCE_INDEX = (
    "<!doctype html><html><head>"
    '<script type="module" src="/src/main.ts"></script>'
    "</head><body>app</body></html>"
)
_BUILT_ABSOLUTE_INDEX = (
    "<!doctype html><html><head>"
    '<script type="module" src="/assets/app.js"></script>'
    "</head><body>spa</body></html>"
)


def _write_zip(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    zp = tmp_path / name
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, content in files.items():
            zf.writestr(rel, content)
    return zp


def _vite_python_files() -> dict[str, str]:
    return {
        "backend/app.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "backend/requirements.txt": "fastapi\nuvicorn\n",
        "frontend/index.html": _SOURCE_INDEX,
        "frontend/src/main.ts": "console.log(1)\n",
        "frontend/package.json": json.dumps(
            {
                "name": "web",
                "scripts": {"build": "vite build"},
                "devDependencies": {"vite": "^5.0.0"},
                "dependencies": {"vue": "^3.0.0"},
            }
        ),
        "frontend/package-lock.json": '{"lockfileVersion":3}',
        "frontend/vite.config.ts": (
            "export default { base: process.env.VITE_BASE ?? '/' }\n"
        ),
    }


def test_import_path_alias_allows_vite_source_entry(
    workspace, registry, config, tmp_path, monkeypatch
) -> None:
    """issue #142：全新 clone 只有 frontend/index.html（/src/main.ts），import --path-alias 不得误拦。"""
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.detect_backend",
        lambda self: "caddy",
    )
    zip_path = _write_zip(tmp_path, "workbench.zip", _vite_python_files())
    importer = Importer(workspace, config, registry)
    result = importer.import_zip(zip_path, path_alias="funasr-workbench")
    assert result.manifest.runtime == Runtime.DOCKER_COMPOSE
    assert result.manifest.container is not None
    assert result.manifest.container.routeHost == "funasr-workbench"
    assert result.manifest.buildBaseFromAlias is True
    events = registry.list_events(result.instance_id)
    assert any("别名守卫豁免" in (e.get("message") or "") for e in events)


def test_import_path_alias_rejects_prebuilt_assets_even_with_build_script(
    workspace, registry, config, tmp_path, monkeypatch
) -> None:
    """预构建 /assets/ 入口即使同时有 package.json build，也不得因自动 buildBaseFromAlias 放行。"""
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.detect_backend",
        lambda self: "caddy",
    )
    zip_path = _write_zip(
        tmp_path,
        "prebuilt-with-build.zip",
        {
            "index.html": _BUILT_ABSOLUTE_INDEX,
            "assets/app.js": "console.log(1);",
            "package.json": json.dumps(
                {
                    "name": "prebuilt",
                    "scripts": {"build": "vite build"},
                    "devDependencies": {"vite": "^5.0.0"},
                }
            ),
        },
    )
    importer = Importer(workspace, config, registry)
    with pytest.raises(RecognitionError, match="绝对路径资源|白屏|IMP-023"):
        importer.import_zip(zip_path, path_alias="prebuilt-spa")
    assert not workspace.app_dir("prebuilt-with-build").exists()


def test_import_path_alias_still_rejects_prebuilt_absolute_spa(
    workspace, registry, config, tmp_path, monkeypatch
) -> None:
    """预构建产物已含绝对 /assets/ 时，导入期硬守卫仍拒绝（不能全局取消）。"""
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.detect_backend",
        lambda self: "caddy",
    )
    zip_path = _write_zip(
        tmp_path,
        "spa.zip",
        {
            "index.html": _BUILT_ABSOLUTE_INDEX,
            "assets/app.js": "console.log(1);",
        },
    )
    importer = Importer(workspace, config, registry)
    with pytest.raises(RecognitionError, match="绝对路径资源|白屏|IMP-023"):
        importer.import_zip(zip_path, path_alias="broken-spa")
    assert not workspace.app_dir("spa").exists()


@pytest.mark.parametrize("marker", [
    "<!-- original source: /src/main.ts -->",
    "<p>original source: /src/main.ts</p>",
    '<script>const original = "/src/main.ts";</script>',
    '<img src="/src/main.ts">',
])
def test_prebuilt_source_marker_does_not_bypass_alias_guard(
    workspace, registry, config, tmp_path, monkeypatch, marker
) -> None:
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.detect_backend", lambda self: "caddy"
    )
    files = _vite_python_files()
    files["frontend/index.html"] = marker + _BUILT_ABSOLUTE_INDEX
    files["frontend/assets/app.js"] = "console.log(1)"
    archive = _write_zip(tmp_path, "marker.zip", files)
    with pytest.raises(RecognitionError, match="绝对路径资源|白屏|IMP-023"):
        Importer(workspace, config, registry).import_zip(archive, path_alias="demo")
    assert not workspace.app_dir("marker").exists()


@pytest.mark.parametrize("extra", [
    '<script type="module" src="/assets/app.js"></script>',
    '<link rel="stylesheet" href="/assets/app.css">',
])
def test_source_entry_does_not_exempt_other_absolute_resources(
    workspace, registry, config, tmp_path, monkeypatch, extra
) -> None:
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.detect_backend", lambda self: "caddy"
    )
    files = _vite_python_files()
    files["frontend/index.html"] = _SOURCE_INDEX + extra
    archive = _write_zip(tmp_path, "mixed.zip", files)
    with pytest.raises(RecognitionError, match="绝对路径资源|白屏|IMP-023"):
        Importer(workspace, config, registry).import_zip(archive, path_alias="demo")


def test_source_exemption_requires_existing_source_file(
    workspace, registry, config, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.detect_backend", lambda self: "caddy"
    )
    files = _vite_python_files()
    del files["frontend/src/main.ts"]
    archive = _write_zip(tmp_path, "missing.zip", files)
    with pytest.raises(RecognitionError, match="绝对路径资源|白屏|IMP-023"):
        Importer(workspace, config, registry).import_zip(archive, path_alias="demo")
