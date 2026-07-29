"""``lwa workspace`` 子命令组（IMP-042 / DEV-089）：工作区迁移。

唯一执行入口：``lwa workspace relocate``。交互与排障见 Skill
``lwa-relocate-workspace``；人工逃生舱见 ``docs/workspace-rename.md``。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from local_webpage_access.cli._common import log
from local_webpage_access.errors import LwaError, MigrateError

app = typer.Typer(help="工作区级操作（迁移 / relocate）")


@app.command("relocate")
def workspace_relocate(
    new_path: str | None = typer.Argument(
        None,
        help="目标工作区绝对/相对路径（--resume/--verify/--rollback 时可省略）",
    ),
    from_path: str | None = typer.Option(
        None,
        "--from",
        help="显式旧工作区根（默认：当前定位到的工作区）",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="只跑预检与计划，零副作用"
    ),
    as_json: bool = typer.Option(False, "--json", help="机器可读 JSON 输出"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="跳过确认（非 TTY 必须）"
    ),
    snapshot_out: Path | None = typer.Option(
        None,
        "--snapshot-out",
        help="把迁移快照额外写到工作区外路径",
    ),
    resume: bool = typer.Option(
        False, "--resume", help="从 journal 失败阶段继续"
    ),
    verify: bool = typer.Option(
        False, "--verify", help="不搬迁，只跑验收不变量"
    ),
    rollback: bool = typer.Option(
        False, "--rollback", help="在 journal 允许时回滚到旧路径（v1 同卷）"
    ),
) -> None:
    """将 LWA 工作区同卷原子迁移到新路径（IMP-042）。"""
    mode_flags = sum(bool(x) for x in (resume, verify, rollback))
    if mode_flags > 1:
        typer.secho(
            "不能同时指定 --resume / --verify / --rollback",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        from local_webpage_access.paths import Workspace, find_workspace_root, require_workspace
        from local_webpage_access.workspace_migrate import read_journal, run_migrate

        def _load_journal(*candidates: Path | None):
            seen: set[Path] = set()
            for cand in candidates:
                if cand is None:
                    continue
                root = cand.expanduser().resolve()
                if root in seen:
                    continue
                seen.add(root)
                if not (root / "local-web.yml").is_file() and not (root / "run").is_dir():
                    continue
                try:
                    journal = read_journal(Workspace(root))
                except Exception:  # noqa: BLE001
                    continue
                if journal and (journal.get("old") or journal.get("new")):
                    return journal
            return None

        if from_path:
            old = Path(from_path).expanduser().resolve()
        elif resume or verify or rollback:
            found = find_workspace_root()
            if found is not None:
                old = found
            elif new_path:
                old = Path(new_path).expanduser().resolve()
            else:
                old = require_workspace().root
        else:
            old = require_workspace().root

        if resume or verify or rollback:
            # BUG-390：即使显式传入 NEW，也必须读 journal 拿权威 old/new
            found = find_workspace_root()
            journal = _load_journal(
                Path(from_path).expanduser().resolve() if from_path else None,
                found,
                Path(new_path).expanduser().resolve() if new_path else None,
                old,
            )
            if journal and journal.get("old") and journal.get("new"):
                old = Path(str(journal["old"]))
                new = Path(str(journal["new"]))
            elif new_path:
                new = Path(new_path).expanduser().resolve()
            elif journal and journal.get("new"):
                new = Path(str(journal["new"]))
                if journal.get("old"):
                    old = Path(str(journal["old"]))
            elif resume or rollback:
                hint = (
                    "无法从 journal 解析目标路径。"
                    "若工作区已搬迁，请传入 NEW（journal 在新路径 run/ 下）；"
                    "或 cd 到新工作区后执行 --resume/--rollback。"
                )
                if from_path:
                    hint += f" 当前 --from={from_path} 未找到含 new 字段的 journal。"
                raise MigrateError(hint)
            else:
                new = old
        else:
            if not new_path:
                typer.secho(
                    "请指定目标路径：lwa workspace relocate <NEW>",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=2)
            new = Path(new_path).expanduser().resolve()

        if (
            not dry_run
            and not yes
            and not resume
            and not verify
            and not rollback
            and not sys.stdin.isatty()
        ):
            typer.secho(
                "非交互环境请加 --yes，或先 --dry-run",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)

        if (
            not dry_run
            and not yes
            and not resume
            and not verify
            and not rollback
            and sys.stdin.isatty()
        ):
            typer.echo(f"将把工作区\n  {old}\n迁移到\n  {new}")
            typer.echo("期间会停业务实例与自启服务；同卷原子改名。")
            if not typer.confirm("确认继续？", default=False):
                raise typer.Exit(code=1)

        result = run_migrate(
            old,
            new,
            dry_run=dry_run,
            yes=yes,
            snapshot_out=snapshot_out,
            resume=resume,
            verify_only=verify,
            rollback=rollback,
        )
    except MigrateError as exc:
        log.error(str(exc), extra=exc.context)
        if as_json:
            typer.echo(
                json.dumps(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": getattr(exc, "code", None),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if as_json:
        typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_result(result)

    if not result.ok:
        raise typer.Exit(code=1)


def _print_result(result) -> None:
    if result.dry_run:
        color = typer.colors.GREEN if result.ok else typer.colors.RED
        typer.secho("── 工作区迁移预演（dry-run）──", fg=color)
        typer.echo(f"  OLD: {result.old}")
        typer.echo(f"  NEW: {result.new}")
        if result.preflight:
            for issue in result.preflight.blocking:
                typer.secho(f"  [BLOCK] {issue.code}: {issue.message}", fg=typer.colors.RED)
            for issue in result.preflight.warnings:
                typer.secho(
                    f"  [WARN] {issue.code}: {issue.message}",
                    fg=typer.colors.YELLOW,
                )
            if result.preflight.path_holders:
                typer.echo(
                    "  路径持有者："
                    + ", ".join(result.preflight.path_holders[:12])
                )
        if result.planned_actions:
            typer.echo("  计划阶段/动作：")
            for a in result.planned_actions[:40]:
                typer.echo(f"    · {a}")
        if result.error:
            typer.secho(f"  错误：{result.error}", fg=typer.colors.RED)
        return

    color = typer.colors.GREEN if result.ok else typer.colors.RED
    typer.secho(
        f"── 工作区迁移（phase={result.phase}）──",
        fg=color,
    )
    typer.echo(f"  OLD: {result.old}")
    typer.echo(f"  NEW: {result.new}")
    if result.started:
        typer.echo(f"  已恢复实例：{', '.join(result.started)}")
    if result.verify_notes:
        for n in result.verify_notes:
            typer.echo(f"  · {n}")
    if result.error:
        typer.secho(f"  错误：{result.error}", fg=typer.colors.RED)
        typer.echo("  可尝试：lwa workspace relocate --resume")
        typer.echo("  或：lwa workspace relocate --rollback")
        typer.echo("  人工逃生舱：docs/workspace-rename.md")
    elif result.ok:
        typer.secho("  完成。建议：cd NEW && lwa workspace relocate --verify", fg=typer.colors.GREEN)
