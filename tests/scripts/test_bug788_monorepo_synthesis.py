"""BUG-788（S2-10 实案）：npm workspaces monorepo 合成错误回归。

真实案例：patent-disclosure-workflow-v1（node ≥24 workspaces monorepo，
包 @patent-disclosure/core / webpage / desktop）。import --update 重扫描后：

1. ``entry.install`` 合成为 ``npm ci``，丢了 ``--ignore-scripts``——root
   package.json 的 ``preinstall``（ensure-electron-mirror.mjs）依赖宿主机
   环境，容器内 ``npm ci`` 执行必败（exit 1，见
   design/5-checkpoints/2026-10-14/S2-11-drills/08-patent-update2.log）。
2. ``entry.start`` 误选 ``packages/webpage/dist/watch``——dev 脚本是
   ``tsx watch src/server.ts``，旧正则把 ``watch`` 标志当成入口文件；
   应为 ``packages/webpage/dist/server.js``。
3. ``internalPort`` 漂移（旧 manifest 17717 -> 新合成 3000）——应用只读
   ``process.env.PORT``（无字面回退端口），推断不可靠；scanner 侧标注
   ``internalPortGuessed``，update 场景由 importer 保留既有端口（BUG-787，
   另一变更处理 importer 侧）。

本文件夹具仿照该布局自建（真实源码不在仓库内）。
"""

from __future__ import annotations

import json
from pathlib import Path

from local_webpage_access.models import Kind
from local_webpage_access.scanner import Scanner

# 仿 packages/webpage 的 server 入口：只读 process.env.PORT（无字面回退），
# 端口完全由 compose 注入的环境决定。
_WEBPAGE_SERVER_ENV_ONLY_TS = """\
import { createServer } from "node:http";

const port = Number(process.env.PORT);

const server = createServer((req, res) => {
  res.end("ok");
});

server.listen(port, () => {
  console.log(`listening on ${port}`);
});
"""

# 带字面回退端口的形式：推断可靠（internalPortGuessed 应为 False）。
_WEBPAGE_SERVER_WITH_FALLBACK_TS = """\
import { createServer } from "node:http";

const PORT = Number(process.env.PORT || 3210);

const server = createServer((req, res) => {
  res.end("ok");
});

server.listen(PORT, () => {
  console.log(`listening on ${PORT}`);
});
"""


def _write_pkg(path: Path, data: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text(json.dumps(data), encoding="utf-8")


def _write_tsconfig(path: Path, out_dir: str = "dist") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"outDir": out_dir}}), encoding="utf-8"
    )


def _write_patent_incident_monorepo(
    root: Path,
    *,
    root_scripts: dict | None = None,
    webpage_start: str = "tsx watch src/server.ts",
    webpage_entry_source: str = _WEBPAGE_SERVER_ENV_ONLY_TS,
    webpage_entry_name: str = "src/server.ts",
    lockfile: str = "package-lock.json",
) -> None:
    """仿 BUG-788 实案的 workspaces monorepo（core + webpage + desktop）。"""
    root_pkg: dict = {
        "name": "patent-disclosure-workflow-v1",
        "private": True,
        "workspaces": ["packages/*"],
    }
    if root_scripts is not None:
        root_pkg["scripts"] = root_scripts
    _write_pkg(root, root_pkg)
    if lockfile:
        (root / lockfile).write_text("{}", encoding="utf-8")
    if root_scripts and "ensure-electron-mirror.mjs" in json.dumps(root_scripts):
        scripts_dir = root / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        (scripts_dir / "ensure-electron-mirror.mjs").write_text(
            "// 依赖宿主机环境的镜像配置脚本，容器内执行必败\n", encoding="utf-8"
        )

    # packages/core：library，有 build 脚本，被 webpage 依赖
    core = root / "packages" / "core"
    _write_pkg(
        core,
        {
            "name": "@patent-disclosure/core",
            "version": "1.0.0",
            "main": "dist/index.js",
            "scripts": {"build": "tsc"},
        },
    )
    _write_tsconfig(core)
    (core / "src").mkdir(parents=True)
    (core / "src" / "index.ts").write_text(
        "export function handleRequest(req: any, res: any): void { res.end('ok'); }\n",
        encoding="utf-8",
    )

    # packages/webpage：node:http 服务，dev 形态 start（tsx watch）
    webpage = root / "packages" / "webpage"
    _write_pkg(
        webpage,
        {
            "name": "@patent-disclosure/webpage",
            "version": "1.0.0",
            "dependencies": {"@patent-disclosure/core": "*"},
            "scripts": {
                "build": "tsc",
                "dev": webpage_start,
                "start": webpage_start,
            },
        },
    )
    _write_tsconfig(webpage)
    (webpage / "src").mkdir(parents=True)
    (webpage / webpage_entry_name).write_text(webpage_entry_source, encoding="utf-8")

    # packages/desktop：electron_desktop，不参与部署
    desktop = root / "packages" / "desktop"
    _write_pkg(
        desktop,
        {
            "name": "@patent-disclosure/desktop",
            "version": "1.0.0",
            "devDependencies": {"electron": "^37.0.0"},
        },
    )


# ---- 问题 1：install 补 --ignore-scripts -------------------------------------


def test_monorepo_install_ignores_scripts_when_root_has_preinstall(tmp_path: Path) -> None:
    """root 有 preinstall（依赖宿主机环境）时 install 应补 --ignore-scripts。"""
    _write_patent_incident_monorepo(
        tmp_path,
        root_scripts={"preinstall": "node scripts/ensure-electron-mirror.mjs"},
    )

    result = Scanner().detect(tmp_path)

    assert result.pending is False
    assert result.kind == Kind.NODE
    assert result.primary_package == "packages/webpage"
    assert result.entry.install == "npm ci --ignore-scripts"
    assert any("--ignore-scripts" in n for n in result.notes)


def test_monorepo_install_unchanged_without_lifecycle_scripts(tmp_path: Path) -> None:
    """root 无 install 生命周期脚本时 install 保持 npm ci，不加参数。"""
    _write_patent_incident_monorepo(tmp_path, root_scripts={"build": "tsc"})

    result = Scanner().detect(tmp_path)

    assert result.entry.install == "npm ci"


def test_monorepo_install_guard_covers_postinstall_and_prepare(tmp_path: Path) -> None:
    """postinstall / prepare 同属 install 生命周期，也要补 --ignore-scripts。"""
    _write_patent_incident_monorepo(
        tmp_path,
        root_scripts={"prepare": "husky install"},
    )

    result = Scanner().detect(tmp_path)

    assert result.entry.install == "npm ci --ignore-scripts"


def test_monorepo_install_guard_covers_pnpm_lockfile(tmp_path: Path) -> None:
    """pnpm 项目（corepack && 链）追加参数只影响末尾安装子命令。"""
    _write_patent_incident_monorepo(
        tmp_path,
        root_scripts={"preinstall": "node scripts/ensure-electron-mirror.mjs"},
        lockfile="pnpm-lock.yaml",
    )

    result = Scanner().detect(tmp_path)

    assert result.entry.install == "corepack enable && pnpm install --frozen-lockfile --ignore-scripts"


def test_monorepo_install_not_double_guarded(tmp_path: Path) -> None:
    """root 脚本自己已带 --ignore-scripts 时不得重复追加。"""
    _write_patent_incident_monorepo(
        tmp_path,
        root_scripts={"preinstall": "node scripts/ensure-electron-mirror.mjs"},
    )
    # 预先在 root scripts 里塞一个含 --ignore-scripts 的 install（防御重复追加）
    pkg_path = tmp_path / "package.json"
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    pkg["scripts"]["install"] = "npm ci --ignore-scripts"
    pkg_path.write_text(json.dumps(pkg), encoding="utf-8")

    result = Scanner().detect(tmp_path)

    assert result.entry.install == "npm ci --ignore-scripts"


def test_flat_node_install_guarded_on_postinstall(tmp_path: Path) -> None:
    """扁平 Node 项目同样适用：root postinstall -> npm ci --ignore-scripts。"""
    _write_pkg(
        tmp_path,
        {
            "name": "flat-node",
            "dependencies": {"express": "^4.18.0"},
            "scripts": {
                "postinstall": "node ./scripts/host-only.mjs",
                "start": "node server.js",
            },
        },
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    result = Scanner().detect(tmp_path)

    assert result.pending is False
    assert result.entry.install == "npm ci --ignore-scripts"


# ---- 问题 2：start 排除 watch/dev 类入口 -------------------------------------


def test_monorepo_start_skips_tsx_watch_flag(tmp_path: Path) -> None:
    """``tsx watch src/server.ts`` 不得把 watch 当入口合成 dist/watch。"""
    _write_patent_incident_monorepo(tmp_path, webpage_start="tsx watch src/server.ts")

    result = Scanner().detect(tmp_path)

    assert result.entry.start == "node packages/webpage/dist/server.js"


def test_monorepo_start_prefers_server_entry_over_devish_file(tmp_path: Path) -> None:
    """入口名含 dev（src/dev.ts）时优先回退到 server/index/main 生产入口。"""
    _write_patent_incident_monorepo(tmp_path, webpage_start="tsx src/dev.ts")
    (tmp_path / "packages" / "webpage" / "src" / "dev.ts").write_text(
        "console.log('dev helper');\n", encoding="utf-8"
    )

    result = Scanner().detect(tmp_path)

    assert result.entry.start == "node packages/webpage/dist/server.js"


def test_monorepo_start_prefers_main_when_no_server_entry(tmp_path: Path) -> None:
    """无 server 入口时按 server -> main -> index 顺序选生产入口。"""
    _write_patent_incident_monorepo(
        tmp_path,
        webpage_start="tsx src/hot-reload.ts",
        webpage_entry_name="src/hot-reload.ts",
    )
    (tmp_path / "packages" / "webpage" / "src" / "main.ts").write_text(
        _WEBPAGE_SERVER_ENV_ONLY_TS, encoding="utf-8"
    )

    result = Scanner().detect(tmp_path)

    assert result.entry.start == "node packages/webpage/dist/main.js"


def test_monorepo_start_keeps_devish_entry_when_no_alternative(tmp_path: Path) -> None:
    """入口名含 dev 且无更优入口时沿用原推断（不无故回退 dev 模式）。"""
    _write_patent_incident_monorepo(
        tmp_path,
        webpage_start="tsx src/dev.ts",
        webpage_entry_name="src/dev.ts",
    )

    result = Scanner().detect(tmp_path)

    assert result.entry.start == "node packages/webpage/dist/dev.js"


# ---- 问题 3：internalPort 弱信号标注 -----------------------------------------


def test_monorepo_env_only_port_marked_guessed(tmp_path: Path) -> None:
    """应用只读 process.env.PORT（无字面回退）时 internalPortGuessed=True。"""
    _write_patent_incident_monorepo(
        tmp_path, webpage_entry_source=_WEBPAGE_SERVER_ENV_ONLY_TS
    )

    result = Scanner().detect(tmp_path)

    assert result.internalPort == 3000  # 兜底默认
    assert result.internalPortGuessed is True
    assert any("保留 manifest 既有端口" in n for n in result.notes)


def test_monorepo_port_with_literal_fallback_not_guessed(tmp_path: Path) -> None:
    """``process.env.PORT || 3210`` 有字面回退，推断可靠，不标 guessed。"""
    _write_patent_incident_monorepo(
        tmp_path, webpage_entry_source=_WEBPAGE_SERVER_WITH_FALLBACK_TS
    )

    result = Scanner().detect(tmp_path)

    assert result.internalPort == 3210
    assert result.internalPortGuessed is False


def test_flat_stdlib_env_only_port_marked_guessed(tmp_path: Path) -> None:
    """扁平 node-stdlib-http 项目同样标注 env-only 端口推断。"""
    _write_pkg(
        tmp_path,
        {
            "name": "flat-node-http",
            "dependencies": {},
            "scripts": {},
        },
    )
    (tmp_path / "server.js").write_text(
        "const http = require('node:http');\n"
        "const port = Number(process.env.PORT);\n"
        "http.createServer((q, s) => s.end('ok')).listen(port);\n",
        encoding="utf-8",
    )

    result = Scanner().detect(tmp_path)

    assert result.pending is False
    assert result.internalPort == 3000
    assert result.internalPortGuessed is True
