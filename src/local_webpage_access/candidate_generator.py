"""Layer 1：候选生成（IMP-058 Gate-B / Gate-C C.02）。

根据 Layer 0 的 :class:`ProjectEvidence`，生成：

- **Gate-B**：一组有序的 :class:`DeploymentCandidate`（扁平候选，向后兼容）。
- **Gate-C**：一组有序的 :class:`DeploymentPlan`（部署拓扑），每个计划内含协作组件
  和能力契约。只有能力契约等价的计划才是可互相降级的替代候选。

候选生成规则按优先级排列，执行后按 ``confidenceTier`` 排序（primary > alternate > fallback）。

规则表（§6.3）：
  R-python-subdir   子目录有 Python Web 框架依赖 -> python/docker-compose, sourceSubdir=<subdir>
  R-python-root     根目录有 Python Web 框架依赖 -> python/docker-compose, sourceSubdir=None
  R-node-monorepo   根 package.json 有 workspaces（IMP-057 规则集）
  R-node-root       根目录有 package.json + NODE_BACKEND
  R-frontend-subdir 子目录有 package.json + NODE_FRONTEND + scripts.build
  R-frontend-root   根目录有 package.json + NODE_FRONTEND + scripts.build
  R-static          根目录或子目录有 index.html
  R-static-fallback 有 index.html 但同时存在后端候选 -> fallback（仅诊断线索）

Gate-C C.02 关键变化：当后端候选存在时，前端不再作为 fallback 候选，
而是纳入同一 :class:`DeploymentPlan` 的构建组件。
"""

from __future__ import annotations

from local_webpage_access.logging import get_logger
from local_webpage_access.models import (
    CapabilityContract,
    CommandSpec,
    DeploymentCandidate,
    DeploymentComponent,
    DeploymentPlan,
    EntryConfig,
    ProjectEvidence,
    SubdirSignal,
)

log = get_logger("candidate_generator")

# 复用 scanner 的框架常量（避免重复定义）
_PYTHON_WEB = {
    "flask", "fastapi", "uvicorn", "gunicorn", "django",
    "streamlit", "gradio", "starlette", "sanic", "tornado",
}
_NODE_FRONTEND = {"vite", "react", "react-dom", "vue", "@vitejs/plugin-react", "svelte", "preact"}
_NODE_BACKEND = {
    "express", "fastify", "koa", "@nestjs/core", "next", "nuxt",
    "@hono/node-server", "polka", "restana",
}
# Python 框架默认端口
_PYTHON_PORT = {
    "flask": 5000, "fastapi": 8000, "uvicorn": 8000, "gunicorn": 8000,
    "django": 8000, "streamlit": 8501, "gradio": 7860,
    "starlette": 8000, "sanic": 8000, "tornado": 8888,
}
_PYTHON_PRIORITY = (
    "fastapi", "flask", "django", "streamlit", "gradio", "uvicorn",
    "gunicorn", "starlette", "sanic", "tornado",
)


def _is_node_backend_start_script(script: str | None) -> bool:
    """保守识别 package.json 中明确的 Node 长驻服务启动命令。"""
    normalized = " ".join((script or "").strip().lower().split())
    if not normalized:
        return False
    frontend_or_dev = (
        "vite",
        "vite preview",
        "react-scripts start",
        "webpack serve",
        "next dev",
        "nuxt dev",
    )
    if any(normalized == item or normalized.startswith(f"{item} ") for item in frontend_or_dev):
        return False
    backend_commands = (
        "node ",
        "tsx ",
        "ts-node ",
        "nest start",
        "next start",
        "nuxt start",
    )
    return normalized.startswith(backend_commands)


def generate_candidates(evidence: ProjectEvidence) -> list[DeploymentCandidate]:
    """根据证据生成有序候选列表。

    返回的列表按 ``confidenceTier`` 排序：primary > alternate > fallback。
    同 tier 内按生成顺序（即优先级表顺序）。
    """
    candidates: list[DeploymentCandidate] = []

    # ---- Python 候选 ----
    # R-python-subdir: 检查子目录中的 Python Web 框架
    for signal in evidence.subdirSignals:
        web_frameworks = set(signal.pythonDeps) & _PYTHON_WEB
        if web_frameworks:
            candidate = _make_python_subdir_candidate(signal, web_frameworks)
            candidates.append(candidate)

    # R-python-root: 根目录 Python Web 框架
    root_python_web = set(evidence.pythonDeps) & _PYTHON_WEB
    if root_python_web and evidence.hasPackageJson is False or (
        root_python_web and not any(
            s for s in evidence.subdirSignals
            if set(s.pythonDeps) & _PYTHON_WEB
        )
    ):
        # 仅当根目录有 Python Web 框架且没有子目录 Python 候选时生成
        if not any(c.kind == "python" and c.sourceSubdir is not None for c in candidates):
            candidate = _make_python_root_candidate(evidence, root_python_web)
            candidates.append(candidate)

    # ---- Node monorepo 候选 ----
    # R-node-monorepo: 由 IMP-057 package_classifier 处理
    # 在 scanner.detect() 中已集成，此处不重复

    # ---- Node 子目录候选 ----
    # R-node-subdir: 子目录有 NODE_BACKEND 依赖（如 server/package.json 含 express）
    for signal in evidence.subdirSignals:
        if not signal.hasPackageJson:
            continue
        node_backend = set(signal.nodeDeps.keys()) & _NODE_BACKEND
        if node_backend or _is_node_backend_start_script(signal.nodeScripts.get("start")):
            candidate = _make_node_subdir_candidate(signal, node_backend)
            candidates.append(candidate)

    # ---- Node 根目录候选 ----
    # R-node-root: 根目录有 NODE_BACKEND
    if evidence.hasPackageJson:
        node_backend = set(evidence.nodeDeps.keys()) & _NODE_BACKEND
        if node_backend and not any(c.kind == "node" for c in candidates):
            candidate = _make_node_root_candidate(evidence, node_backend)
            candidates.append(candidate)

    # ---- Frontend 候选 ----
    # R-frontend-subdir: 子目录有 NODE_FRONTEND + build script
    has_backend_candidate = any(c.kind in ("python", "node") and c.confidenceTier == "primary" for c in candidates)
    for signal in evidence.subdirSignals:
        frontend_deps = set(signal.nodeDeps.keys()) & _NODE_FRONTEND
        if frontend_deps and "build" in signal.nodeScripts:
            if has_backend_candidate:
                candidate = _make_frontend_subdir_candidate(signal, tier="alternate")
            else:
                candidate = _make_frontend_subdir_candidate(signal, tier="primary")
            candidates.append(candidate)

    # R-frontend-root: 根目录有 NODE_FRONTEND + build script
    if evidence.hasPackageJson:
        frontend_deps = set(evidence.nodeDeps.keys()) & _NODE_FRONTEND
        if frontend_deps and "build" in evidence.nodeScripts:
            if not any(c.kind == "node" and c.confidenceTier == "primary" for c in candidates):
                if has_backend_candidate:
                    candidate = _make_frontend_root_candidate(evidence, tier="alternate")
                else:
                    candidate = _make_frontend_root_candidate(evidence, tier="primary")
                candidates.append(candidate)

    # ---- Static 候选 ----
    # R-static / R-static-fallback
    if evidence.hasIndexHtml or evidence.hasHtml:
        if candidates:
            # 有更高优先级候选 -> static 作为 fallback
            candidate = DeploymentCandidate(
                kind="static",
                runtime="shared_static",
                servingMode="shared_static",
                form="static",
                resourceProfile="tiny",
                entry=EntryConfig(),
                confidenceTier="fallback",
                reasoning=["根目录有 index.html，但存在更高优先级候选，作为 fallback"],
            )
        else:
            candidate = DeploymentCandidate(
                kind="static",
                runtime="shared_static",
                servingMode="shared_static",
                form="static",
                resourceProfile="tiny",
                entry=EntryConfig(),
                confidenceTier="primary",
                reasoning=["根目录有 index.html，无后端/前端工程文件"],
            )
        candidates.append(candidate)

    # ---- 排序 ----
    tier_order = {"primary": 0, "alternate": 1, "fallback": 2}
    candidates.sort(key=lambda c: tier_order.get(c.confidenceTier, 99))

    return candidates


# ---- Python 候选生成 --------------------------------------------------------


def _make_python_subdir_candidate(
    signal: SubdirSignal,
    web_frameworks: set[str],
) -> DeploymentCandidate:
    """R-python-subdir：子目录有 Python Web 框架。"""
    framework = _pick_framework(web_frameworks)
    port = _PYTHON_PORT.get(framework, 8000)
    has_sqlite = any(
        dep in {"sqlite3", "sqlalchemy", "peewee", "tortoise-orm", "aiosqlite"}
        for dep in signal.pythonDeps
    )

    entry = EntryConfig(
        install="pip install -r requirements.txt",
        build=None,
        start=_python_start_command(framework, signal.path, has_sqlite),
    )

    reasoning = [
        f"子目录 {signal.path}/ 有 Python Web 框架依赖：{', '.join(sorted(web_frameworks))}",
        f"requirements.txt 在 {signal.path}/ 子目录",
    ]

    return DeploymentCandidate(
        kind="python",
        runtime="docker_compose",
        servingMode="container",
        form="fullstack-sqlite" if has_sqlite else "backend-container",
        resourceProfile="small",
        entry=entry,
        internalPort=port,
        sourceSubdir=signal.path,
        confidenceTier="primary",
        reasoning=reasoning,
    )


def _make_python_root_candidate(
    evidence: ProjectEvidence,
    web_frameworks: set[str],
) -> DeploymentCandidate:
    """R-python-root：根目录有 Python Web 框架。"""
    framework = _pick_framework(web_frameworks)
    port = _PYTHON_PORT.get(framework, 8000)
    has_sqlite = any(
        dep in {"sqlite3", "sqlalchemy", "peewee", "tortoise-orm", "aiosqlite"}
        for dep in evidence.pythonDeps
    )

    entry = EntryConfig(
        install="pip install -r requirements.txt",
        build=None,
        start=_python_start_command(framework, None, has_sqlite),
    )

    return DeploymentCandidate(
        kind="python",
        runtime="docker_compose",
        servingMode="container",
        form="fullstack-sqlite" if has_sqlite else "backend-container",
        resourceProfile="small",
        entry=entry,
        internalPort=port,
        sourceSubdir=None,
        confidenceTier="primary",
        reasoning=[f"根目录有 Python Web 框架依赖：{', '.join(sorted(web_frameworks))}"],
    )


def _python_start_command(framework: str, subdir: str | None, has_sqlite: bool) -> str:
    """推断 Python 启动命令。"""
    # 如果在子目录，uvicorn target 需要基于子目录的模块路径
    # 但具体模块路径（app.main:app）由 scanner._infer_uvicorn_app_target 推断
    # 这里给出基础命令，scanner 会进一步修正
    if framework in ("fastapi", "uvicorn", "starlette", "sanic"):
        # uvicorn target 由 scanner 的 _infer_uvicorn_app_target 推断
        # 这里给出默认值，scanner 会覆盖
        target = "app.main:app" if subdir is None else f"{subdir}.app.main:app"
        # 简化：使用子目录名作为模块前缀
        return f"uvicorn {target} --host 0.0.0.0 --port 8000"
    elif framework == "flask":
        return "flask run --host 0.0.0.0 --port 5000"
    elif framework == "django":
        return "python manage.py runserver 0.0.0.0:8000"
    elif framework == "streamlit":
        return "streamlit run app.py --server.port 8501"
    elif framework == "gradio":
        return "python app.py"
    elif framework == "gunicorn":
        return "gunicorn app.main:app -b 0.0.0.0:8000"
    elif framework == "tornado":
        return "python app.py"
    return "python app.py"


def _pick_framework(frameworks: set[str]) -> str:
    """按固定优先级选择主导框架（BUG-181）。"""
    for fw in _PYTHON_PRIORITY:
        if fw in frameworks:
            return fw
    return next(iter(frameworks))


# ---- Node 候选生成 ----------------------------------------------------------


def _make_node_root_candidate(
    evidence: ProjectEvidence,
    backend_deps: set[str],
) -> DeploymentCandidate:
    """R-node-root：根目录有 NODE_BACKEND。"""
    has_build = "build" in evidence.nodeScripts
    has_start = "start" in evidence.nodeScripts

    entry = EntryConfig(
        install=_node_install_command(evidence),
        build="npm run build" if has_build else None,
        start="npm run start" if has_start else "node server.js",
    )

    is_heavy = any(d in ("next", "nuxt") for d in backend_deps)

    return DeploymentCandidate(
        kind="node",
        runtime="docker_compose",
        servingMode="container",
        form="backend-container",
        resourceProfile="medium" if is_heavy else "small",
        entry=entry,
        internalPort=3000 if "next" in backend_deps else 8080,
        sourceSubdir=None,
        confidenceTier="primary",
        reasoning=[f"根目录有 Node 后端依赖：{', '.join(sorted(backend_deps))}"],
    )


def _make_node_subdir_candidate(
    signal: SubdirSignal,
    backend_deps: set[str],
) -> DeploymentCandidate:
    """R-node-subdir：子目录有 NODE_BACKEND（如 backend/package.json 含 express）。"""
    has_build = "build" in signal.nodeScripts
    has_start = "start" in signal.nodeScripts
    has_lock = bool(signal.nodeDeps)  # 简化：有依赖即用 npm ci

    entry = EntryConfig(
        install="npm ci" if has_lock else "npm install",
        build="npm run build" if has_build else None,
        start="npm run start" if has_start else "node server.js",
    )

    is_heavy = any(d in ("next", "nuxt") for d in backend_deps)

    return DeploymentCandidate(
        kind="node",
        runtime="docker_compose",
        servingMode="container",
        form="backend-container",
        resourceProfile="medium" if is_heavy else "small",
        entry=entry,
        internalPort=3000 if "next" in backend_deps else 8080,
        sourceSubdir=signal.path,
        confidenceTier="primary",
        reasoning=[
            f"子目录 {signal.path}/ 有 Node 后端依赖：{', '.join(sorted(backend_deps))}",
            f"package.json 在 {signal.path}/ 子目录",
        ],
    )


# ---- Frontend 候选生成 ------------------------------------------------------


def _make_frontend_subdir_candidate(
    signal: SubdirSignal,
    tier: str = "primary",
) -> DeploymentCandidate:
    """R-frontend-subdir：子目录有 NODE_FRONTEND + build script。"""
    entry = EntryConfig(
        install="npm ci",
        build="npm run build",
        start=None,
        buildOutputDir=f"{signal.path}/dist",
    )

    return DeploymentCandidate(
        kind="node",
        runtime="shared_static",
        servingMode="shared_static",
        form="frontend-static",
        resourceProfile="tiny",
        entry=entry,
        internalPort=None,
        sourceSubdir=signal.path,
        confidenceTier=tier,
        reasoning=[
            f"子目录 {signal.path}/ 有前端框架依赖",
            f"有 build 脚本：{signal.nodeScripts.get('build', '')}",
        ],
    )


def _make_frontend_root_candidate(
    evidence: ProjectEvidence,
    tier: str = "primary",
) -> DeploymentCandidate:
    """R-frontend-root：根目录有 NODE_FRONTEND + build script。"""
    entry = EntryConfig(
        install=_node_install_command(evidence),
        build="npm run build",
        start=None,
    )

    return DeploymentCandidate(
        kind="node",
        runtime="shared_static",
        servingMode="shared_static",
        form="frontend-static",
        resourceProfile="tiny",
        entry=entry,
        internalPort=None,
        sourceSubdir=None,
        confidenceTier=tier,
        reasoning=["根目录有前端框架依赖 + build 脚本"],
    )


# ---- 辅助 --------------------------------------------------------------------


def _node_install_command(evidence: ProjectEvidence) -> str:
    """推断 Node 安装命令。"""
    root_files = set(evidence.rootFiles)
    if "pnpm-lock.yaml" in root_files:
        return "pnpm install --frozen-lockfile"
    if "yarn.lock" in root_files:
        return "yarn install --frozen-lockfile"
    if "package-lock.json" in root_files or "bun.lockb" in root_files:
        return "npm ci"
    return "npm install"


# ---- Gate-C C.02：部署拓扑生成 ----------------------------------------------


def generate_plans(evidence: ProjectEvidence) -> list[DeploymentPlan]:
    """Gate-C C.02：将扁平候选收敛为 :class:`DeploymentPlan`。

    与 :func:`generate_candidates` 的核心区别（§6.1.1）：
    - 当后端候选存在时，前端不再是 fallback 候选，而是纳入同一计划的**构建组件**。
    - 全栈项目（backend + frontend）收敛为**一个**计划（不是两个可降级候选）。
    - static 候选若与后端信号共存，标记为 ``diagnostic``（不参与自动启动）。
    - 能力契约由结构化证据派生，用于降级等价性校验。

    返回的列表按 ``confidenceTier`` 排序：primary > alternate > diagnostic。
    """
    plans: list[DeploymentPlan] = []
    candidates = generate_candidates(evidence)

    # 识别后端候选与前端候选
    backend_cands = [
        c for c in candidates
        if c.kind in ("python", "node")
        and c.runtime == "docker_compose"
        and c.confidenceTier == "primary"
    ]
    frontend_cands = [
        c for c in candidates
        if c.kind == "node"
        and c.runtime == "shared_static"
        and c.form == "frontend-static"
    ]
    static_cands = [
        c for c in candidates
        if c.kind == "static"
    ]

    if backend_cands:
        # 有后端候选：每个后端候选可能与其前端候选共同构成全栈计划
        for backend in backend_cands:
            components: list[DeploymentComponent] = []
            contract = CapabilityContract()
            reasoning: list[str] = list(backend.reasoning)
            evidence_refs: list[str] = []

            # 后端运行组件
            backend_component = _candidate_to_component(
                backend, role="runtime",
            )
            components.append(backend_component)

            # 能力契约：后端 → servesApi + requiresDatabase（若检测到）
            contract.servesApi = True
            if backend.form == "fullstack-sqlite":
                contract.requiresDatabase = True
                # 有 alembic.ini 证据 → 需要迁移
                backend_signal = next(
                    (
                        signal
                        for signal in evidence.subdirSignals
                        if signal.path == backend.sourceSubdir
                    ),
                    None,
                )
                if evidence.hasAlembicIni or (
                    backend_signal is not None and backend_signal.hasAlembicIni
                ):
                    contract.requiresMigrations = True
            contract.requiresDatabase = contract.requiresDatabase or bool(
                evidence.sqliteFiles
            )
            evidence_refs.append("backend_candidate")

            # 查找可协作的前端候选（纳入同一计划）
            cooperative_frontends = [
                f for f in frontend_cands
                if f.confidenceTier == "alternate"
            ]
            for frontend in cooperative_frontends:
                frontend_component = _candidate_to_component(
                    frontend, role="build",
                )
                # C.03：前端构建产物传递到后端静态目录
                frontend_component.artifactTarget = "backend/static"
                components.append(frontend_component)
                contract.servesUi = True
                reasoning.append(
                    f"前端构建组件（{frontend.sourceSubdir or 'root'}）"
                    f"产物传递到 backend/static"
                )
                evidence_refs.append("frontend_candidate")

            # CHK-193/P1：source="guessed" 的探针保持可选（isMandatory=False）。
            # Flask/Django/Express 等普通后端不保证提供 /health 端点，
            # 404 不应导致部署失败。只有源码声明或发现的端点才能作为门槛。
            from local_webpage_access.models import ProbeSpec
            contract.requiredProbes.append(ProbeSpec(
                path="/health",
                isMandatory=False,
                source="guessed",
                description="诊断探针（HTTP GET /health，可选；不通过仅产生告警）",
            ))

            plan = DeploymentPlan(
                planId=f"plan-{'fullstack' if contract.servesUi else 'backend'}-"
                       f"{backend.sourceSubdir or 'root'}",
                components=components,
                capabilityContract=contract,
                confidenceTier="primary",
                reasoning=reasoning,
                evidenceRefs=evidence_refs,
            )
            plans.append(plan)

        # static 候选作为诊断线索（不参与自动启动）
        if static_cands:
            static_plan = DeploymentPlan(
                planId="plan-static-diagnostic",
                components=[_candidate_to_component(static_cands[0], role="runtime")],
                capabilityContract=CapabilityContract(servesUi=True),
                confidenceTier="diagnostic",
                reasoning=[
                    "存在 index.html 但同时检测到后端信号——",
                    "static 仅作为诊断线索，不作为运行时 fallback（§6.1.1 能力守恒）",
                ],
                evidenceRefs=["static_index_html"],
            )
            plans.append(static_plan)
    else:
        # 无后端候选：前端/静态候选独立成为计划
        for frontend in frontend_cands:
            if frontend.confidenceTier == "primary":
                components = [_candidate_to_component(frontend, role="build-and-runtime")]
                contract = CapabilityContract(servesUi=True)
                from local_webpage_access.models import ProbeSpec
                contract.requiredProbes.append(ProbeSpec(
                    path="/",
                    isMandatory=False,
                    source="guessed",
                    description="诊断探针（静态首页存活，可选；liveness 已覆盖）",
                ))
                plan = DeploymentPlan(
                    planId=f"plan-frontend-{frontend.sourceSubdir or 'root'}",
                    components=components,
                    capabilityContract=contract,
                    confidenceTier="primary",
                    reasoning=frontend.reasoning,
                    evidenceRefs=["frontend_candidate"],
                )
                plans.append(plan)

        for static in static_cands:
            if static.confidenceTier == "primary":
                components = [_candidate_to_component(static, role="runtime")]
                contract = CapabilityContract(servesUi=True)
                from local_webpage_access.models import ProbeSpec
                contract.requiredProbes.append(ProbeSpec(
                    path="/",
                    isMandatory=False,
                    source="guessed",
                    description="诊断探针（静态首页存活，可选；liveness 已覆盖）",
                ))
                plan = DeploymentPlan(
                    planId="plan-static",
                    components=components,
                    capabilityContract=contract,
                    confidenceTier="primary",
                    reasoning=static.reasoning,
                    evidenceRefs=["static_index_html"],
                )
                plans.append(plan)

    # 排序：primary > alternate > diagnostic
    tier_order = {"primary": 0, "alternate": 1, "fallback": 2, "diagnostic": 3}
    plans.sort(key=lambda p: tier_order.get(p.confidenceTier, 99))
    return plans


def _candidate_to_component(
    candidate: DeploymentCandidate,
    *,
    role: str,
) -> DeploymentComponent:
    """将 :class:`DeploymentCandidate` 转换为 :class:`DeploymentComponent`。

    Gate-C C.03：将 entry 字符串转换为 :class:`CommandSpec`（结构化命令）。
    """
    component = DeploymentComponent(
        componentId=f"{candidate.kind}-{role}",
        role=role,
        sourceSubdir=candidate.sourceSubdir,
        internalPort=candidate.internalPort,
    )

    # 转换 entry 字符串为 CommandSpec
    if candidate.entry.start:
        # C.03：检测 shell 操作符，决定用 argv 还是 shell 模式
        from local_webpage_access.dockerfile_templates import _has_shell_operators
        if _has_shell_operators(candidate.entry.start):
            component.startCommand = CommandSpec(
                shell=candidate.entry.start,
                workdir="/app" if not candidate.sourceSubdir else f"/app/{candidate.sourceSubdir}",
            )
        else:
            import shlex
            component.startCommand = CommandSpec(
                argv=shlex.split(candidate.entry.start),
                workdir="/app" if not candidate.sourceSubdir else f"/app/{candidate.sourceSubdir}",
            )

    if candidate.entry.build:
        component.buildCommand = CommandSpec(
            shell=candidate.entry.build,
            workdir="/app" if not candidate.sourceSubdir else f"/app/{candidate.sourceSubdir}",
        )

    if candidate.entry.buildOutputDir:
        component.buildOutputDir = candidate.entry.buildOutputDir

    return component
