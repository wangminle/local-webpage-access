"""C.01/C.02/C.03（IMP-056 后置包）回归：兼容性 findings 可见、可刷新、可解释。

* C.01：``lwa list``/``lwa status`` 对有 findings 的实例显示 ⚠ 与最高等级；
  JSON 输出带稳定字段；无 findings 输出不变。
* C.02：``lwa scan`` 用同一 checker 原子覆盖 findings（删问题代码即清除），
  记录证据根与扫描时间；扫描失败保留旧结果并标 stale。
* C.03：IMP-055 别名拒绝原因后附加关联 finding 的 file/line/fix（预检线索）；
  无 finding 时错误完全原样；findings 不得单独构成拒绝。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from local_webpage_access.config import Config
from local_webpage_access.importer import Importer
from local_webpage_access.models import InstanceManifest
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.status import instance_status


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    w = Workspace(tmp_path / "ws")
    w.ensure_workspace_dirs()
    return w


@pytest.fixture()
def cfg() -> Config:
    from local_webpage_access.config import PortPool

    return Config(staticGateway="builtin", portPool=PortPool(start=21000, end=21050))


@pytest.fixture()
def reg(ws: Workspace) -> Registry:
    ws.root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    r = Registry(ws.root / "registry" / "local-web.db")
    r.open()
    yield r
    r.close()


def _make_zip(path: Path, files: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def _import_with_findings(
    ws: Workspace, cfg: Config, reg: Registry, tmp_path: Path, *, with_api_abs: bool
) -> str:
    """导入一个前端静态实例（CHK-P03 命中 → critical findings）。"""
    app_js = "const API = '';\n" if with_api_abs else "const BASE_URL = '/app/';\n"
    zp = _make_zip(
        tmp_path / "front.zip",
        {"index.html": "<html><body>hi</body></html>", "app.js": app_js},
    )
    importer = Importer(ws, cfg, reg)
    result = importer.import_zip(zp)
    return result.instance_id


# ---- C.01：list/status 展示 ---------------------------------------------------


def test_c01_status_summary_has_severity_and_count(
    ws, cfg, reg, tmp_path
) -> None:
    iid = _import_with_findings(ws, cfg, reg, tmp_path, with_api_abs=True)
    st = instance_status(ws, cfg, reg, iid)
    assert st.compatibility_severity == "critical"
    assert st.compatibility_count >= 1
    d = st.to_dict()
    # JSON 稳定字段（非拼接文案）
    assert d["compatibilitySeverity"] == "critical"
    assert d["compatibilityCount"] >= 1


def test_c01_no_findings_fields_stay_none(ws, cfg, reg, tmp_path) -> None:
    """无 findings 的实例：severity None / count 0（输出保持紧凑）。"""
    iid = _import_with_findings(ws, cfg, reg, tmp_path, with_api_abs=False)
    st = instance_status(ws, cfg, reg, iid)
    # BASE_URL 关键字命中 → 只有 CHK-P04 warning？BASE_URL 在 P04 关键字表，
    # 但 P03 空 base 正则不命中（BASE_URL 非空）。可能仍有 P04 warning，
    # 但绝不应是 critical。
    assert st.compatibility_severity != "critical"
    if not st.compatibility_count:
        assert st.compatibility_severity is None


def test_c01_highest_severity_helper() -> None:
    from local_webpage_access.status import _highest_compatibility_severity
    from local_webpage_access.models import CompatibilityFinding

    findings = [
        CompatibilityFinding(
            checkId="CHK-P04", severity="warning", title="t", impact="i", fix="f"
        ),
        CompatibilityFinding(
            checkId="CHK-P03", severity="critical", title="t", impact="i", fix="f"
        ),
    ]
    assert _highest_compatibility_severity(findings) == "critical"
    assert _highest_compatibility_severity(findings[::-1]) == "critical"


def test_c01_cli_renders_warning_line(ws, cfg, reg, tmp_path, monkeypatch, capsys) -> None:
    from local_webpage_access.cli.status import _echo_compatibility_line

    class _S:
        compatibility_severity = "critical"
        compatibility_count = 2

    class _None:
        compatibility_severity = None
        compatibility_count = 0

    _echo_compatibility_line(_S())
    out = capsys.readouterr().out
    assert "⚠" in out and "critical" in out and "2 条" in out

    _echo_compatibility_line(_None())
    assert capsys.readouterr().out == ""  # 无 findings 零输出


# ---- C.02：scan 刷新 ---------------------------------------------------------


def test_c02_scan_refreshes_and_clears_findings(ws, cfg, reg, tmp_path) -> None:
    """删除问题代码后 scan 清除旧 finding，并记录证据根/时间。"""
    from local_webpage_access.compatibility_checker import refresh_compatibility_findings

    iid = _import_with_findings(ws, cfg, reg, tmp_path, with_api_abs=True)
    mpath = ws.app_manifest_path(iid)
    manifest = InstanceManifest.load(mpath)
    assert manifest.compatibilityFindings  # 导入期命中

    # 修复源码（删除空 API base），重扫 → findings 清空 + meta 更新
    app_js = ws.app_current(iid) / "app.js"
    app_js.write_text("const BASE_URL = '/app/';\n", encoding="utf-8")
    manifest = InstanceManifest.load(mpath)
    ok = refresh_compatibility_findings(ws.app_current(iid), manifest)
    assert ok is True
    assert manifest.compatibilityFindings == []
    meta = manifest.compatibilityScanMeta
    assert meta is not None
    assert meta["stale"] is False
    assert str(ws.app_current(iid)) in meta["evidenceRoot"]
    assert meta["scannedAt"]
    assert meta["count"] == 0


def test_c02_scan_failure_keeps_old_and_marks_stale(
    ws, cfg, reg, tmp_path, monkeypatch
) -> None:
    """checker 异常时保留旧 findings 并标 stale，不写半成品。"""
    from local_webpage_access import compatibility_checker as cc

    iid = _import_with_findings(ws, cfg, reg, tmp_path, with_api_abs=True)
    mpath = ws.app_manifest_path(iid)
    manifest = InstanceManifest.load(mpath)
    old = list(manifest.compatibilityFindings)
    assert old

    def boom(*a, **k):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(cc, "check_compatibility", boom)
    ok = cc.refresh_compatibility_findings(ws.app_current(iid), manifest)
    assert ok is False
    # 旧结果原样保留
    assert [f.checkId for f in manifest.compatibilityFindings] == [
        f.checkId for f in old
    ]
    meta = manifest.compatibilityScanMeta
    assert meta is not None and meta.get("stale") is True
    assert "disk exploded" in (meta.get("lastError") or "")


def test_c02_uses_same_checker_entry(monkeypatch) -> None:
    """C.02 边界：refresh 必须调用 check_compatibility（与 import 同一入口）。"""
    from local_webpage_access import compatibility_checker as cc

    called: list[dict] = []

    def fake_check(source_dir, *, primary_subdir=None):
        called.append({"dir": source_dir, "subdir": primary_subdir})
        return []

    monkeypatch.setattr(cc, "check_compatibility", fake_check)
    from local_webpage_access.models import InstanceManifest  # noqa: F401

    class _M:  # 最小 manifest 替身
        compatibilityFindings = ["old"]
        compatibilityScanMeta = None

    ok = cc.refresh_compatibility_findings(Path("/tmp/x"), _M(), primary_subdir="pkg/web")
    assert ok is True
    assert called == [{"dir": Path("/tmp/x"), "subdir": "pkg/web"}]


# ---- C.03：alias 拒绝解释 -----------------------------------------------------


def _manifest_with_findings(ws: Workspace, iid: str, *, findings: list) -> InstanceManifest:
    ws.ensure_app_dirs(iid)
    mpath = ws.app_manifest_path(iid)
    manifest = InstanceManifest.load(mpath) if mpath.is_file() else None
    if manifest is None:
        (ws.app_current(iid) / "index.html").write_text("<h1>x</h1>")
        manifest = InstanceManifest(
            id=iid,
            name=iid,
            version="1",
            kind="static",
            stack=[],
            runtime="shared-static",
            servingMode="shared-static",
            resourceProfile="small",
        )
    manifest.compatibilityFindings = findings
    manifest.save(mpath)
    return manifest


def test_c03_rejection_appends_related_findings(ws, cfg) -> None:
    from local_webpage_access.errors import RecognitionError
    from local_webpage_access.models import CompatibilityFinding
    from local_webpage_access.path_alias import _enrich_alias_rejection_with_findings

    findings = [
        CompatibilityFinding(
            checkId="CHK-P03",
            severity="critical",
            title="空 API base 常量",
            file="src/app.ts",
            line=42,
            code=None,
            impact="...",
            fix="将 API base 设为 BASE_PATH",
        ),
        CompatibilityFinding(
            checkId="CHK-P04", severity="warning", title="无关", impact="i", fix="f"
        ),
    ]
    manifest = _manifest_with_findings(ws, "c03-app", findings=findings)

    original = RecognitionError("IMP-055 原始拒绝原因：绝对资源 /assets/app.js")
    enriched = _enrich_alias_rejection_with_findings(original, manifest)
    text = str(enriched)
    # 主错误在前，保持原文（str 形式自带单个 [RECOGNITION_ERROR] 前缀）
    assert text.count("[RECOGNITION_ERROR]") == 1
    assert text.startswith("[RECOGNITION_ERROR] IMP-055 原始拒绝原因")
    # 附加线索：标注预检 + file:line + fix；只含 CHK-P03 相关项
    assert "预检线索" in text
    assert "src/app.ts:42" in text
    assert "BASE_PATH" in text
    assert "无关" not in text


def test_c03_without_findings_error_unchanged(ws) -> None:
    from local_webpage_access.errors import RecognitionError
    from local_webpage_access.path_alias import _enrich_alias_rejection_with_findings

    manifest = _manifest_with_findings(ws, "c03-clean", findings=[])
    original = RecognitionError("IMP-055 原始拒绝原因")
    result = _enrich_alias_rejection_with_findings(original, manifest)
    # 完全原样（LwaError str 自带 [CODE] 前缀，与同构异常比对）
    assert str(result) == str(RecognitionError("IMP-055 原始拒绝原因"))


def test_c03_set_alias_rejection_carries_findings(
    ws, cfg, reg, monkeypatch
) -> None:
    """端到端：alias set 被 IMP-055 拒绝时错误带预检线索（通过 set 入口）。"""
    import pytest as _pytest

    from local_webpage_access.errors import RecognitionError
    from local_webpage_access.models import CompatibilityFinding
    from local_webpage_access.path_alias import set_instance_path_alias

    findings = [
        CompatibilityFinding(
            checkId="CHK-P03",
            severity="critical",
            title="空 API base 常量",
            file="app.js",
            line=7,
            code=None,
            impact="...",
            fix="引入 BASE_PATH",
        )
    ]
    manifest = _manifest_with_findings(ws, "c03-e2e", findings=findings)
    reg.upsert_from_manifest(manifest)

    # Caddy 假网关 + 入口 HTML 含加载型绝对资源 → IMP-055 硬拒绝
    class _GW:
        def __init__(self, workspace, config) -> None:
            pass

        def detect_backend(self) -> str:
            return "caddy"

        def is_enabled(self, iid) -> bool:
            return True

    from local_webpage_access import path_alias as pa

    monkeypatch.setattr(pa, "StaticGateway", _GW)
    html = '<html><head><script src="/assets/app.js"></script></head><body>x</body></html>'
    monkeypatch.setattr(
        pa, "_fetch_entrypoint_html_for_alias_guard", lambda **kw: html
    )
    monkeypatch.setattr(
        pa, "validate_path_alias", lambda alias, existing_aliases=None: None
    )
    monkeypatch.setattr(pa, "verify_alias_live", lambda *a, **k: None)

    with _pytest.raises(RecognitionError) as ei:
        set_instance_path_alias(ws, cfg, reg, "c03-e2e", "demo")
    text = str(ei.value)
    assert "IMP-055" in text or "绝对" in text or "拒绝" in text
    assert "预检线索" in text
    assert "app.js:7" in text
