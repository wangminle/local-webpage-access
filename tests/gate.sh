#!/usr/bin/env bash
# LWA 本地门禁（PLN-052 S0-02）。
#
# 依次执行：
#   1. ruff check src tests          （规则集固定在 pyproject，见 ADJ-033）
#   2. pytest                        （含 Caddyfile 语义测试；缺 caddy 时这些用例跳过）
#   3. git diff --check / --cached   （尾随空白、冲突标记）
#
# 每一步都会执行，最后汇总；任一步失败则退出码非 0。后续工作包以本脚本全绿为完成条件。
#
# 用法：
#   bash tests/gate.sh             # 全量（Windows 用 Git Bash）
#   bash tests/gate.sh -k alias -x # 额外参数原样透传给 pytest
#
# 环境变量：
#   LWA_GATE_PYTHON   指定 Python 解释器（默认依次尝试 .venv、uv run、python3、python）
#   LWA_TEST_CADDY    指定 caddy 可执行文件，只用于 Caddyfile 语义测试（tests/scripts/_caddy.py）
#   LWA_GATE_JOBS     pytest 并行 worker 数（DEV-166）：默认 3（控制资源占用）；1 或 0 强制串行，auto 用全部核
#                     （调试 -s / --pdb 时用）。需要 pytest-xdist，缺失时自动退回串行。
#                     分组策略见 tests/scripts/conftest.py 的 _REAL_PORT_FILES：占用固定测试
#                     端口池的文件固定同组串行，其余每个文件独立成组。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

# ---- 选择解释器 ----------------------------------------------------------------
# 只接受“真能跑起来”的候选：跨平台同步的 .venv 可能是另一台机器的符号链接。
PY=()
_try_python() {
    "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)' >/dev/null 2>&1
}
if [[ -n "${LWA_GATE_PYTHON:-}" ]]; then
    PY=("$LWA_GATE_PYTHON")
else
    for candidate in ".venv/bin/python" ".venv/Scripts/python.exe"; do
        if [[ -e "$candidate" ]] && _try_python "$candidate"; then
            PY=("$candidate")
            break
        fi
    done
    if [[ ${#PY[@]} -eq 0 ]] && command -v uv >/dev/null 2>&1 \
        && _try_python uv run --extra dev python; then
        PY=(uv run --extra dev python)
    fi
    if [[ ${#PY[@]} -eq 0 ]]; then
        for candidate in python3 python; do
            if command -v "$candidate" >/dev/null 2>&1 && _try_python "$candidate"; then
                PY=("$candidate")
                break
            fi
        done
    fi
fi
if [[ ${#PY[@]} -eq 0 ]]; then
    echo "gate: 找不到 Python >= 3.13（可用 LWA_GATE_PYTHON 指定）" >&2
    exit 2
fi

echo "gate: python = ${PY[*]} ($("${PY[@]}" -c 'import sys; print(sys.version.split()[0])'))"
if [[ -n "${LWA_TEST_CADDY:-}" ]]; then
    echo "gate: caddy  = $LWA_TEST_CADDY（LWA_TEST_CADDY）"
elif command -v caddy >/dev/null 2>&1; then
    echo "gate: caddy  = $(command -v caddy)"
else
    echo "gate: caddy  = 未找到 —— Caddyfile 语义测试将跳过（CI 中必须可用）"
fi

# ---- 执行各步骤 ----------------------------------------------------------------
declare -a RESULTS=()
FAILED=0

run_step() {
    local name="$1"
    shift
    echo
    echo "==> [$name] $*"
    local start=$SECONDS
    if "$@"; then
        RESULTS+=("PASS  $name ($((SECONDS - start))s)")
    else
        RESULTS+=("FAIL  $name ($((SECONDS - start))s)")
        FAILED=1
    fi
}

# ---- pytest 并行参数 -----------------------------------------------------------
PYTEST_PAR=()
JOBS="${LWA_GATE_JOBS:-3}"
if [[ "$JOBS" != "0" && "$JOBS" != "1" ]]; then
    if "${PY[@]}" -c 'import xdist' >/dev/null 2>&1; then
        PYTEST_PAR=(-n "$JOBS" --dist loadgroup)
        echo "gate: pytest 并行 = -n $JOBS --dist loadgroup（LWA_GATE_JOBS=1 可强制串行）"
    else
        echo "gate: 未安装 pytest-xdist —— pytest 串行执行（uv sync --extra dev 可启用并行）"
    fi
else
    echo "gate: pytest 串行（LWA_GATE_JOBS=$JOBS）"
fi

run_step "ruff" "${PY[@]}" -m ruff check src tests
run_step "pytest" env LC_ALL=C LANG=C "${PY[@]}" -m pytest "${PYTEST_PAR[@]}" "$@"
run_step "diff-check(工作区)" git diff --check
run_step "diff-check(暂存区)" git diff --cached --check

echo
echo "==== gate 汇总 ===="
for line in "${RESULTS[@]}"; do
    echo "  $line"
done
if [[ $FAILED -ne 0 ]]; then
    echo "gate: 未通过"
    exit 1
fi
echo "gate: 全绿"
