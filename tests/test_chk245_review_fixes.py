"""全量代码审阅报告（2026-08-14）候选疑点批量收敛的回归测试。

对应 2026-08-19 处置批次：审阅报告 §三 的候选疑点经逐项核实后，本文件锁定
其中"确认为真缺陷且已修复"项的行为契约。逐项处置结论（修复/不修理由）见
`design/2-achievement/全量代码审阅报告-20260814.md` 附录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---- 安全审计（组 7）----------------------------------------------------------


class TestSecurityAudit:
    def test_add_remote_url_detects_uppercase_scheme(self) -> None:
        """评审-组7：`ADD HTTPS://…` 此前绕过 critical 检测。"""
        from local_webpage_access.security import audit_dockerfile

        findings = audit_dockerfile("ADD HTTPS://EVIL.EXAMPLE/x.tar /app\n")
        kinds = {f.code for f in findings}
        assert "add_remote_url" in kinds

    def test_dollar_var_relative_bind_source_flagged(self) -> None:
        """评审-组7：`./${HOME}` 相对路径内嵌变量按不可展开处理（敏感命中 critical）。"""
        import yaml

        from local_webpage_access.security import audit_compose

        compose_text = yaml.safe_dump(
            {"services": {"app": {"image": "demo:latest", "volumes": ["./${HOME}/:/app/host"]}}}
        )
        findings = audit_compose(compose_text)
        kinds = {f.code for f in findings}
        # BUG-184 语义：${HOME} 非路径片段命中 → warn（不静默按命名卷放行）。
        # 修复前 `./${HOME}` 前缀带 ./ 连 warn 都没有。
        assert "unexpected_host_mount" in kinds


# ---- 数据层（组 7）------------------------------------------------------------


class TestDataLayer:
    def test_instance_id_rejects_trailing_newline(self) -> None:
        """评审-组7：`$` 接受结尾换行，`abc\\n` 可生成带换行的目录名。"""
        from local_webpage_access.errors import LwaError
        from local_webpage_access.paths import validate_instance_id

        with pytest.raises(LwaError):
            validate_instance_id("abc\n")

    def test_manifest_save_atomic_no_tmp_leftover(self, tmp_path: Path) -> None:
        """评审-组7：save 走临时文件 + os.replace，不留 .json.tmp。"""
        from local_webpage_access.models import InstanceManifest, Kind, Runtime, ServingMode

        manifest = InstanceManifest(
            id="demo",
            name="Demo",
            version="1",
            kind=Kind.STATIC,
            runtime=Runtime.SHARED_STATIC,
            servingMode=ServingMode.SHARED_STATIC,
        )
        path = tmp_path / "local-web.json"
        manifest.save(path)
        manifest.save(path)  # 二次保存同样原子
        assert path.is_file()
        assert not (tmp_path / "local-web.json.tmp").exists()
        assert InstanceManifest.load(path).id == "demo"


# ---- 版本门禁（组 6）----------------------------------------------------------


class TestVersionGate:
    def test_prerelease_not_ge_release(self) -> None:
        """评审-组6：semver 预发布小于同号正式版，最低版本门禁不再放行。"""
        from local_webpage_access.version_requirements import version_ge

        assert version_ge("2.40.2-rc1", "2.40.2") is False
        assert version_ge("1.0.0-alpha", "1.0.0") is False
        assert version_ge("2.40.2", "2.40.2") is True
        assert version_ge("2.40.3", "2.40.2") is True
        assert version_ge("2.41.0-rc1", "2.40.2") is True  # 主版本更高仍放行


# ---- API 参数校验（组 4）------------------------------------------------------


class TestApiValidation:
    @staticmethod
    def _app():
        from local_webpage_access.config import Config, PortPool
        from local_webpage_access.paths import Workspace

        ws = Workspace(Path("/tmp/lwa-chk245-ws"))
        return ws, Config(portPool=PortPool(start=22000, end=22050))

    def test_body_bool_string_false_not_truthy(self) -> None:
        """评审-组4：`"dryRun": "false"` 字符串不再被 bool() 判 True。

        评审-P1 追加：键存在但非 bool 一律 400，不再回落默认——否则
        default=True 的字段（restart/keepData）收到 "false" 仍为 True。
        """
        import pytest
        from fastapi import HTTPException

        from local_webpage_access.manager_api import _body_bool

        assert _body_bool({"dryRun": True}, "dryRun", False) is True
        assert _body_bool({}, "dryRun", True) is True
        with pytest.raises(HTTPException) as exc_info:
            _body_bool({"dryRun": "false"}, "dryRun", False)
        assert exc_info.value.status_code == 400
        with pytest.raises(HTTPException) as exc_info:
            _body_bool({"restart": 1}, "restart", False)
        assert exc_info.value.status_code == 400  # 非 bool 拒绝，不回落默认


# ---- daemon（组 4）------------------------------------------------------------


class TestDaemonStable:
    def test_future_mtime_treated_as_stable(self, tmp_path: Path) -> None:
        """评审-组4：mtime 在未来（cp -p / 时钟偏移）不再永久"未稳定"。"""
        import os
        import time

        from local_webpage_access.daemon import is_file_stable

        f = tmp_path / "x.zip"
        f.write_bytes(b"data")
        future = time.time() + 3600
        os.utime(f, (future, future))
        prev_st, prev = is_file_stable(f, None)
        stable, _ = is_file_stable(f, prev)
        assert stable is True


# ---- doctor（组 5）------------------------------------------------------------


class TestDoctorFixes:
    def test_default_runner_maps_oserror_to_127(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """评审-组5：可执行文件存在但不可执行时不再炸穿 run_doctor。"""
        import subprocess

        from local_webpage_access import doctor

        def raise_perm(cmd, **kwargs):  # noqa: ANN001, ANN003
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(doctor.subprocess, "run", raise_perm)
        result = doctor._default_runner(["git", "--version"])
        assert result.returncode == 127
        assert isinstance(result, subprocess.CompletedProcess)


# ---- pageviews（组 8）---------------------------------------------------------


class TestPageviewsFixes:
    def test_copytruncate_resets_cursor(self, tmp_path: Path) -> None:
        """评审-组8：无归档原地截断后游标重置，新内容不再永久漏计。"""
        from local_webpage_access.pageviews import PageviewStore, _read_new_lines

        store = PageviewStore(tmp_path / "pv.db")
        log = tmp_path / "gateway.log"
        log.write_text("l1\nl2\nl3\n", encoding="utf-8")
        cursor_key = "builtin:demo:gateway"
        store.set_cursor(cursor_key, log.stat().st_size, "")

        # 模拟 copytruncate：文件被截断后写入了新内容（总大小小于旧游标）
        log.write_text("n1\n", encoding="utf-8")
        batch = _read_new_lines(log, cursor_key, store)
        assert "n1" in batch.lines
        assert batch.next_offset == log.stat().st_size

    def test_ts_string_rfc3339_parsed(self) -> None:
        """评审-组8：字符串时间戳不再一律回退 now_iso（分桶漂移）。"""
        from local_webpage_access.pageviews import _parse_ts_string

        assert _parse_ts_string("2026-08-19T10:00:00Z").startswith("2026-08-19")
        assert _parse_ts_string("1760000000") is not None
        assert _parse_ts_string("garbage") is None


# ---- access（组 8）------------------------------------------------------------


class TestAccessFixes:
    def test_normalize_script_src_folds_parent_segments(self) -> None:
        """评审-组8：`../assets/x.js` 折叠为 `/assets/x.js` 而非 `/../assets/x.js`。"""
        from local_webpage_access.access import _normalize_script_src

        assert _normalize_script_src("../assets/x.js") == "/assets/x.js"
        assert _normalize_script_src("./a.js") == "/a.js"
        assert _normalize_script_src("a.js") == "/a.js"
        assert _normalize_script_src("/abs.js") == "/abs.js"


# ---- autostart（组 5）---------------------------------------------------------


class TestAutostartUnescape:
    def test_systemd_args_unescape_percent(self) -> None:
        """评审-组5：读侧还原写侧的 %% 转义，含 % 路径不再误报 FAIL。"""
        from local_webpage_access.autostart import _unescape_systemd_args

        assert _unescape_systemd_args(["--workspace", "/root/ws%%prod"]) == [
            "--workspace",
            "/root/ws%prod",
        ]


# ---- stats（组 8）-------------------------------------------------------------


class TestStatsParse:
    def test_parse_size_malformed_number_returns_none(self) -> None:
        """评审-组8：`1.5.2MiB` 不再抛 ValueError。"""
        from local_webpage_access.stats import _parse_size

        assert _parse_size("1.5.2MiB") is None
        assert _parse_size("12.5MiB") == int(12.5 * 1024 * 1024)


# ---- 迁移（组 6）--------------------------------------------------------------


class TestMigrateFixes:
    def _manifest(self, tmp_path: Path, app_path: str) -> Path:
        payload = {
            "schemaVersion": 1,
            "id": "demo",
            "name": "Demo",
            "version": "1",
            "kind": "static",
            "runtime": "shared-static",
            "servingMode": "shared-static",
            "appPath": app_path,
        }
        p = tmp_path / "local-web.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return p

    def test_rewrite_manifest_paths_reports_real_change(self, tmp_path: Path) -> None:
        """评审-组6：返回真实变更标志；未变化不再无谓重写。"""
        from local_webpage_access.workspace_migrate import rewrite_manifest_paths

        old, new = "/srv/lwa", "/srv/lwa2"
        p = self._manifest(tmp_path, f"{old}/apps/demo/current")
        assert rewrite_manifest_paths(p, old, new) is True
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["appPath"] == f"{new}/apps/demo/current"
        # 幂等重跑：无变化 → False
        assert rewrite_manifest_paths(p, old, new) is False
        assert rewrite_manifest_paths(p, new, new) is False
