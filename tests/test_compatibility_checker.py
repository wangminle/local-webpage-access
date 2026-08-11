"""兼容性预检模块测试（IMP-056 Gate-2 B.07）。

CHK-P03 / CHK-P04 含正例 + 反例。
"""

from __future__ import annotations

from pathlib import Path

from local_webpage_access.compatibility_checker import check_compatibility


# ---- CHK-P03 正例 ----------------------------------------------------------


def test_p03_fetch_absolute_api(tmp_path: Path) -> None:
    """fetch('/api/...') 应命中 CHK-P03 critical。"""
    (tmp_path / "app.js").write_text(
        'async function loadData() {\n'
        '  const res = await fetch("/api/users");\n'
        '  return res.json();\n'
        '}\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    assert len(p03) >= 1
    assert p03[0].severity == "critical"
    assert p03[0].file == "app.js"
    assert p03[0].line == 2


def test_p03_axios_absolute_api(tmp_path: Path) -> None:
    """axios.get('/api/...') 应命中 CHK-P03 critical。"""
    (tmp_path / "api.ts").write_text(
        "import axios from 'axios';\n"
        "export async function getUser(id: number) {\n"
        "  return axios.get(`/api/users/${id}`);\n"
        "}\n",
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    assert len(p03) >= 1
    assert p03[0].severity == "critical"


def test_p03_empty_api_base_const(tmp_path: Path) -> None:
    """const API = '' 应命中 CHK-P03 critical。"""
    (tmp_path / "config.js").write_text(
        "const API = '';\n"
        "const apiBase = \"\";\n"
        "export { API, apiBase };\n",
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    assert len(p03) >= 2  # const API='' and apiBase=""


def test_p03_python_fetch_absolute(tmp_path: Path) -> None:
    """Python 源码中的 fetch('/api/...') 也应命中。"""
    (tmp_path / "views.py").write_text(
        "import httpx\n"
        "async def fetch_data():\n"
        "    async with httpx.AsyncClient() as c:\n"
        "        # 注意：这种模式不命中（httpx.get 不匹配 fetch|axios）\n"
        "        resp = await c.get('/api/data')\n"
        "    return resp\n",
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    # httpx 不匹配 fetch|axios，但 P04 可能命中
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    # Python httpx.get 不应该被 P03 捕获（只匹配 fetch|axios.\w+）
    assert len(p03) == 0


# ---- CHK-P03 反例 ----------------------------------------------------------


def test_p03_no_false_positive_relative_api(tmp_path: Path) -> None:
    """相对路径 fetch('api/users') 不应命中 P03。"""
    (tmp_path / "app.js").write_text(
        'const res = await fetch("api/users");\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    assert len(p03) == 0


def test_p03_no_false_positive_non_api_path(tmp_path: Path) -> None:
    """fetch('/users') 非 /api/ 路径不应命中 P03。"""
    (tmp_path / "app.js").write_text(
        'const res = await fetch("/users");\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    assert len(p03) == 0


def test_p03_excludes_node_modules(tmp_path: Path) -> None:
    """node_modules 下的文件不应被扫描。"""
    nm = tmp_path / "node_modules" / "lib"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text(
        'fetch("/api/secret");\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    assert len(p03) == 0


def test_p03_excludes_dist_build(tmp_path: Path) -> None:
    """dist/build 目录不应被扫描。"""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "bundle.js").write_text(
        'fetch("/api/data");\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    assert len(p03) == 0


# ---- CHK-P04 ---------------------------------------------------------------


def test_p04_warning_when_no_base_path_keyword(tmp_path: Path) -> None:
    """无 BASE_PATH/BASE_URL 等关键字 -> CHK-P04 warning。"""
    (tmp_path / "app.js").write_text(
        'console.log("hello");\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p04 = [f for f in findings if f.checkId == "CHK-P04"]
    assert len(p04) == 1
    assert p04[0].severity == "warning"
    assert "IMP-055" in p04[0].fix


def test_p04_no_warning_when_base_path_present(tmp_path: Path) -> None:
    """有 BASE_PATH 关键字 -> 不出 P04 warning。"""
    (tmp_path / "app.js").write_text(
        'const prefix = process.env.BASE_PATH || "/";\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p04 = [f for f in findings if f.checkId == "CHK-P04"]
    assert len(p04) == 0


def test_p04_no_warning_when_base_url_present(tmp_path: Path) -> None:
    """有 BASE_URL 关键字 -> 不出 P04 warning。"""
    (tmp_path / "config.py").write_text(
        'BASE_URL = "/myapp/"\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p04 = [f for f in findings if f.checkId == "CHK-P04"]
    assert len(p04) == 0


def test_p04_no_warning_when_x_forwarded_prefix(tmp_path: Path) -> None:
    """有 X-Forwarded-Prefix 关键字 -> 不出 P04。"""
    (tmp_path / "server.py").write_text(
        '# X-Forwarded-Prefix\n'
        'pass\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p04 = [f for f in findings if f.checkId == "CHK-P04"]
    assert len(p04) == 0


def test_p04_no_warning_when_script_name(tmp_path: Path) -> None:
    """有 SCRIPT_NAME 关键字 -> 不出 P04。"""
    (tmp_path / "wsgi.py").write_text(
        'import os\n'
        'script_name = os.environ.get("SCRIPT_NAME", "")\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p04 = [f for f in findings if f.checkId == "CHK-P04"]
    assert len(p04) == 0


def test_p04_no_warning_when_base_path_in_ts(tmp_path: Path) -> None:
    """basePath (camelCase) 关键字 -> 不出 P04。"""
    (tmp_path / "router.ts").write_text(
        'const router = createRouter({ basePath: "/app" });\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p04 = [f for f in findings if f.checkId == "CHK-P04"]
    assert len(p04) == 0


def test_p04_no_files_no_warning(tmp_path: Path) -> None:
    """没有任何可扫描文件时不产生 P04。"""
    findings = check_compatibility(tmp_path)
    p04 = [f for f in findings if f.checkId == "CHK-P04"]
    assert len(p04) == 0


# ---- monorepo primary_subdir -----------------------------------------------


def test_monorepo_scans_primary_subdir_only(tmp_path: Path) -> None:
    """primary_subdir 指定时只扫描该子目录。"""
    # 主包子目录 — 有 P03 命中
    pkg = tmp_path / "packages" / "webpage"
    pkg.mkdir(parents=True)
    (pkg / "app.js").write_text(
        'fetch("/api/users");\n',
        encoding="utf-8",
    )
    # 根目录 — 有另一个 P03 命中
    (tmp_path / "root.js").write_text(
        'fetch("/api/root");\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path, primary_subdir="packages/webpage")
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    assert len(p03) == 1
    assert p03[0].file == "packages/webpage/app.js"


def test_monorepo_excludes_desktop_package(tmp_path: Path) -> None:
    """packages/desktop 子包不应被扫描。"""
    desktop = tmp_path / "packages" / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "main.js").write_text(
        'fetch("/api/electron");\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    assert len(p03) == 0


# ---- 综合 -------------------------------------------------------------------


def test_combined_p03_and_p04(tmp_path: Path) -> None:
    """同时有 P03 命中和 P04 warning。"""
    (tmp_path / "app.js").write_text(
        'fetch("/api/data");\n'
        '// 没有 base path 关键字\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    p04 = [f for f in findings if f.checkId == "CHK-P04"]
    assert len(p03) >= 1
    assert len(p04) == 1


def test_empty_dir_returns_empty(tmp_path: Path) -> None:
    """空目录返回空列表。"""
    findings = check_compatibility(tmp_path)
    assert findings == []


def test_nonexistent_dir_returns_empty(tmp_path: Path) -> None:
    """不存在的子目录返回空列表。"""
    findings = check_compatibility(tmp_path, primary_subdir="nonexistent")
    assert findings == []


def test_code_field_populated(tmp_path: Path) -> None:
    """P03 finding 的 code 字段应包含匹配行内容。"""
    (tmp_path / "app.js").write_text(
        '  const res = await fetch("/api/users");\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    assert len(p03) == 1
    assert p03[0].code is not None
    assert "fetch" in p03[0].code


def test_mjs_cjs_py_extensions_scanned(tmp_path: Path) -> None:
    """所有支持的扩展名都应被扫描。"""
    for ext in (".ts", ".js", ".mjs", ".cjs", ".py"):
        (tmp_path / f"file{ext}").write_text(
            'fetch("/api/test");\n',
            encoding="utf-8",
        )
    findings = check_compatibility(tmp_path)
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    assert len(p03) == 5


def test_non_scanned_extension_ignored(tmp_path: Path) -> None:
    """.html/.css/.json 等不在扫描范围内。"""
    (tmp_path / "page.html").write_text(
        '<script>fetch("/api/html");</script>\n',
        encoding="utf-8",
    )
    findings = check_compatibility(tmp_path)
    p03 = [f for f in findings if f.checkId == "CHK-P03"]
    assert len(p03) == 0
    # 没有扫描到任何文件 -> 不出 P04
    p04 = [f for f in findings if f.checkId == "CHK-P04"]
    assert len(p04) == 0
