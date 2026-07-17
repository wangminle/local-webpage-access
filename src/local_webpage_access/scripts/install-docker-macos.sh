#!/usr/bin/env bash
# LWA：在 macOS 上安装 Docker Desktop（含 Engine + Compose 插件）。
#
# Linux/Ubuntu Engine 安装见同目录 install-docker-linux.sh（对齐）：
#   https://docs.docker.com/engine/install/ubuntu/
# macOS 官方推荐路径为 Docker Desktop：
#   https://docs.docker.com/desktop/setup/install/mac-install/
#
# 用法：
#   bash install-docker-macos.sh
#   LWA_DOCKER_REGISTRY_MIRRORS='https://xxxx.mirror.aliyuncs.com' bash install-docker-macos.sh
#
# 环境变量：
#   LWA_DOCKER_REGISTRY_MIRRORS  逗号分隔的 registry-mirrors（默认 https://docker.m.daocloud.io；
#                                可换阿里云个人加速器；设 none 跳过写入）
#   LWA_MIN_DOCKER_VERSION       最低 Engine 版本（默认 29.0.0）
#   LWA_MIN_COMPOSE_VERSION      最低 Compose 插件版本（默认 2.40.2）
#   LWA_SKIP_HELLO_WORLD=1       跳过 hello-world 验证
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
MIN_DOCKER_VERSION="${LWA_MIN_DOCKER_VERSION:-29.0.0}"
MIN_COMPOSE_VERSION="${LWA_MIN_COMPOSE_VERSION:-2.40.2}"
# 默认国内镜像拉取加速；设 LWA_DOCKER_REGISTRY_MIRRORS=none 可跳过写入。
_REGISTRY_RAW="${LWA_DOCKER_REGISTRY_MIRRORS:-https://docker.m.daocloud.io}"
if [[ "$_REGISTRY_RAW" == "none" || "$_REGISTRY_RAW" == "-" ]]; then
  DEFAULT_REGISTRY_MIRRORS=""
else
  DEFAULT_REGISTRY_MIRRORS="$_REGISTRY_RAW"
fi

log()  { printf '==> [%s] %s\n' "$SCRIPT_NAME" "$*"; }
warn() { printf '⚠️  [%s] %s\n' "$SCRIPT_NAME" "$*" >&2; }
die()  { printf '❌ [%s] %s\n' "$SCRIPT_NAME" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: install-docker-macos.sh [--help]

通过 Homebrew cask 安装 Docker Desktop（含 Compose）。
若已达标则仍补写默认 registry-mirrors（除非 LWA_DOCKER_REGISTRY_MIRRORS=none）。

参考：https://docs.docker.com/desktop/setup/install/mac-install/
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（试 --help）" ;;
  esac
done

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"
}

version_ge() {
  local a="$1" b="$2" IFS=.
  # shellcheck disable=SC2206
  local -a aa=($a) bb=($b)
  local i x y
  for ((i = 0; i < ${#aa[@]} || i < ${#bb[@]}; i++)); do
    x="${aa[i]:-0}"; y="${bb[i]:-0}"
    x="${x%%[!0-9]*}"; y="${y%%[!0-9]*}"
    x="${x:-0}"; y="${y:-0}"
    if ((10#$x > 10#$y)); then return 0; fi
    if ((10#$x < 10#$y)); then return 1; fi
  done
  return 0
}

extract_semver() {
  [[ "$1" =~ ([0-9]+\.[0-9]+(\.[0-9]+)?) ]] && echo "${BASH_REMATCH[1]}" || echo ""
}

detect_macos() {
  [[ "$(uname -s)" == "Darwin" ]] || die "本脚本仅支持 macOS"
}

already_good() {
  command -v docker >/dev/null 2>&1 || return 1
  local server_raw client_raw engine_ver compose_raw compose_ver
  server_raw="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
  client_raw="$(docker version --format '{{.Client.Version}}' 2>/dev/null || true)"
  engine_ver="$(extract_semver "${server_raw:-$client_raw}")"
  [[ -n "$engine_ver" ]] || return 1
  if ! version_ge "$engine_ver" "$MIN_DOCKER_VERSION"; then
    warn "已安装 Docker $engine_ver，低于最低要求 $MIN_DOCKER_VERSION"
    return 1
  fi
  # daemon 未起时 server 为空但 client 可能达标 —— 提示启动而非重装
  if [[ -z "$server_raw" ]]; then
    warn "检测到 docker CLI，但 Engine 未响应；请先打开 Docker Desktop，无需重装"
    return 0
  fi
  docker compose version >/dev/null 2>&1 || return 1
  compose_raw="$(docker compose version --short 2>/dev/null || docker compose version 2>/dev/null || true)"
  compose_ver="$(extract_semver "$compose_raw")"
  if [[ -z "$compose_ver" ]] || ! version_ge "$compose_ver" "$MIN_COMPOSE_VERSION"; then
    warn "Compose ${compose_ver:-unknown} 低于最低要求 $MIN_COMPOSE_VERSION"
    return 1
  fi
  log "已满足要求：Docker Engine $engine_ver，Compose $compose_ver"
  return 0
}

install_desktop() {
  need_cmd brew
  if brew list --cask docker >/dev/null 2>&1; then
    log "Homebrew cask docker 已安装，尝试升级…"
    brew upgrade --cask docker || true
  else
    log "通过 Homebrew 安装 Docker Desktop（cask docker）…"
    brew install --cask docker
  fi
}

write_daemon_mirrors() {
  local mirrors_csv="${1:-}"
  if [[ -z "$mirrors_csv" ]]; then
    warn "LWA_DOCKER_REGISTRY_MIRRORS=none：跳过 ~/.docker/daemon.json 写入"
    return 0
  fi
  need_cmd python3
  log "合并写入 ~/.docker/daemon.json registry-mirrors…"
  mkdir -p "${HOME}/.docker"
  MIRRORS_CSV="$mirrors_csv" HOME="$HOME" python3 - <<'PY'
import json, os, pathlib, shutil, tempfile

path = pathlib.Path(os.environ["HOME"]) / ".docker" / "daemon.json"
mirrors = [x.strip() for x in os.environ.get("MIRRORS_CSV", "").split(",") if x.strip()]

data = {}
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("root is not an object")
    except Exception as exc:
        bak = path.with_suffix(".json.bak-lwa")
        shutil.copy2(path, bak)
        print(f"警告：无法解析 {path} ({exc})，已备份为 {bak}", flush=True)
        data = {}

existing = data.get("registry-mirrors") or []
if not isinstance(existing, list):
    existing = []
merged = []
for item in [*existing, *mirrors]:
    if item and item not in merged:
        merged.append(item)
data["registry-mirrors"] = merged

path.parent.mkdir(parents=True, exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix="daemon.", suffix=".json", dir=str(path.parent))
os.close(fd)
tmp_path = pathlib.Path(tmp)
tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp_path.chmod(0o644)
tmp_path.replace(path)
print(f"已更新 {path} → registry-mirrors={merged}", flush=True)
PY
  warn "若 Docker Desktop 已在运行，请在设置中 Apply & Restart，或重启 Desktop 使镜像加速生效"
}

open_desktop() {
  if [[ -d "/Applications/Docker.app" ]]; then
    log "启动 Docker Desktop…"
    open -a Docker || warn "无法自动打开 Docker.app，请手动启动"
  else
    warn "未找到 /Applications/Docker.app，请确认 cask 安装成功后手动打开"
  fi
}

wait_for_engine() {
  log "等待 Docker Engine 就绪（最多约 120s）…"
  local i
  for ((i = 1; i <= 60; i++)); do
    if docker info >/dev/null 2>&1; then
      log "Docker Engine 已就绪"
      return 0
    fi
    sleep 2
  done
  warn "等待超时：请确认已完成 Desktop 首次许可/启动，然后重新运行本脚本做校验"
  return 1
}

verify_install() {
  log "校验安装…"
  docker version
  docker compose version

  local server_raw engine_ver compose_raw compose_ver
  server_raw="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
  engine_ver="$(extract_semver "$server_raw")"
  [[ -n "$engine_ver" ]] || die "无法读取 Docker Engine 版本（请先启动 Docker Desktop）"
  version_ge "$engine_ver" "$MIN_DOCKER_VERSION" || die "Engine $engine_ver < 最低要求 $MIN_DOCKER_VERSION"

  compose_raw="$(docker compose version --short 2>/dev/null || docker compose version)"
  compose_ver="$(extract_semver "$compose_raw")"
  [[ -n "$compose_ver" ]] || die "无法读取 Compose 版本"
  version_ge "$compose_ver" "$MIN_COMPOSE_VERSION" || die "Compose $compose_ver < 最低要求 $MIN_COMPOSE_VERSION"

  if [[ "${LWA_SKIP_HELLO_WORLD:-0}" != "1" ]]; then
    log "运行 hello-world 验证（可设 LWA_SKIP_HELLO_WORLD=1 跳过）…"
    if ! docker run --rm hello-world; then
      warn "hello-world 失败（常见于镜像加速未配置）；Engine/Compose 版本已达标"
    fi
  fi

  log "完成：Docker Engine $engine_ver，Compose $compose_ver"
  warn "建议在 Docker Desktop → Settings → General 勾选「Start Docker Desktop when you sign in」（LWA 自启检查会提示）"
}

main() {
  detect_macos

  if already_good; then
    write_daemon_mirrors "$DEFAULT_REGISTRY_MIRRORS"
    log "无需重装，退出"
    exit 0
  fi

  install_desktop
  write_daemon_mirrors "$DEFAULT_REGISTRY_MIRRORS"
  open_desktop
  if wait_for_engine; then
    verify_install
  else
    die "Docker Desktop 已安装但 Engine 未就绪；启动 Desktop 后可再跑：bash $0"
  fi
}

main "$@"
