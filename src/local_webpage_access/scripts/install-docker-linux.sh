#!/usr/bin/env bash
# LWA：在 Ubuntu（含 WSL Ubuntu）上安装 Docker Engine + Compose 插件。
#
# 流程对齐官方文档（apt 仓库方式）：
#   https://docs.docker.com/engine/install/ubuntu/
# 默认将 download.docker.com 替换为阿里云 Docker CE 镜像：
#   https://mirrors.aliyun.com/docker-ce/
#   https://developer.aliyun.com/mirror/docker-ce
#
# 用法：
#   bash install-docker-linux.sh
#   bash install-docker-linux.sh --official          # 使用官方 download.docker.com
#   LWA_DOCKER_REGISTRY_MIRRORS='https://xxxx.mirror.aliyuncs.com' bash install-docker-linux.sh
#
# 环境变量：
#   LWA_DOCKER_APT_MIRROR        apt 仓库根 URL（默认阿里云；--official 时忽略）
#   LWA_DOCKER_REGISTRY_MIRRORS  逗号分隔的 registry-mirrors（默认 https://docker.m.daocloud.io；
#                                可换阿里云个人加速器；设 none 跳过写入）
#   LWA_MIN_DOCKER_VERSION       最低 Engine 版本（默认 29.0.0）
#   LWA_MIN_COMPOSE_VERSION      最低 Compose 插件版本（默认 2.40.2）
#   LWA_SKIP_HELLO_WORLD=1       跳过 hello-world 拉取验证
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
USE_OFFICIAL=0
MIN_DOCKER_VERSION="${LWA_MIN_DOCKER_VERSION:-29.0.0}"
MIN_COMPOSE_VERSION="${LWA_MIN_COMPOSE_VERSION:-2.40.2}"
DEFAULT_APT_MIRROR="https://mirrors.aliyun.com/docker-ce"
OFFICIAL_APT_ROOT="https://download.docker.com"
# 默认国内镜像拉取加速（公共加速；可换阿里云个人加速器 https://xxxx.mirror.aliyuncs.com）。
# 设 LWA_DOCKER_REGISTRY_MIRRORS=none 可跳过写入。
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
Usage: install-docker-linux.sh [--official] [--help]

  --official   使用官方 https://download.docker.com（而非阿里云 docker-ce 镜像）
  --help       显示本帮助

参考：https://docs.docker.com/engine/install/ubuntu/
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --official) USE_OFFICIAL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（试 --help）" ;;
  esac
done

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"
}

version_ge() {
  # 返回 0 表示 $1 >= $2
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

run_sudo() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    need_cmd sudo
    sudo "$@"
  fi
}

detect_ubuntu() {
  [[ -f /etc/os-release ]] || die "未找到 /etc/os-release，本脚本仅支持 Ubuntu（含 WSL Ubuntu）"
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID:-}" != "ubuntu" ]]; then
    die "当前发行版 ID=${ID:-unknown}。本期仅支持 Ubuntu；请参考 https://docs.docker.com/engine/install/"
  fi
  local codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
  [[ -n "$codename" ]] || die "无法解析 Ubuntu 代号（UBUNTU_CODENAME / VERSION_CODENAME）"
  case "$codename" in
    jammy|noble|questing|resolute) ;;
    *)
      warn "代号 '$codename' 不在官方当前列表（jammy/noble/questing/resolute），仍继续尝试安装"
      ;;
  esac
  printf '%s\n' "$codename"
}

already_good() {
  command -v docker >/dev/null 2>&1 || return 1
  local server_raw client_raw engine_ver compose_raw compose_ver
  server_raw="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
  client_raw="$(docker version --format '{{.Client.Version}}' 2>/dev/null || true)"
  engine_ver="$(extract_semver "${server_raw:-$client_raw}")"
  [[ -n "$engine_ver" ]] || return 1
  if ! version_ge "$engine_ver" "$MIN_DOCKER_VERSION"; then
    warn "已安装 Docker $engine_ver，低于最低要求 $MIN_DOCKER_VERSION，将继续安装/升级"
    return 1
  fi
  docker compose version >/dev/null 2>&1 || {
    warn "Docker 已达标但缺少 compose 插件，将继续安装"
    return 1
  }
  compose_raw="$(docker compose version --short 2>/dev/null || docker compose version 2>/dev/null || true)"
  compose_ver="$(extract_semver "$compose_raw")"
  if [[ -z "$compose_ver" ]] || ! version_ge "$compose_ver" "$MIN_COMPOSE_VERSION"; then
    warn "Compose ${compose_ver:-unknown} 低于最低要求 $MIN_COMPOSE_VERSION，将继续安装/升级"
    return 1
  fi
  log "已满足要求：Docker Engine $engine_ver，Compose $compose_ver（≥ $MIN_DOCKER_VERSION / $MIN_COMPOSE_VERSION）"
  return 0
}

uninstall_conflicts() {
  # https://docs.docker.com/engine/install/ubuntu/#uninstall-old-versions
  log "卸载可能冲突的旧包（docker.io / podman-docker 等）…"
  local pkgs
  pkgs="$(dpkg --get-selections docker.io docker-compose docker-compose-v2 docker-doc podman-docker containerd runc 2>/dev/null | cut -f1 || true)"
  if [[ -n "${pkgs}" ]]; then
    # shellcheck disable=SC2086
    run_sudo apt-get remove -y $pkgs || true
  else
    log "未发现冲突包，跳过"
  fi
}

setup_apt_repo() {
  # https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository
  local codename="$1"
  local arch apt_uri gpg_url key_path
  arch="$(dpkg --print-architecture)"

  if [[ "$USE_OFFICIAL" -eq 1 ]]; then
    apt_uri="${OFFICIAL_APT_ROOT}/linux/ubuntu"
    gpg_url="${OFFICIAL_APT_ROOT}/linux/ubuntu/gpg"
    log "配置官方 apt 仓库：$apt_uri"
  else
    apt_uri="${LWA_DOCKER_APT_MIRROR:-$DEFAULT_APT_MIRROR}/linux/ubuntu"
    gpg_url="${LWA_DOCKER_APT_MIRROR:-$DEFAULT_APT_MIRROR}/linux/ubuntu/gpg"
    log "配置阿里云 Docker CE apt 仓库：$apt_uri"
  fi

  log "安装 ca-certificates / curl…"
  run_sudo apt-get update -y
  run_sudo apt-get install -y ca-certificates curl

  run_sudo install -m 0755 -d /etc/apt/keyrings
  key_path=/etc/apt/keyrings/docker.asc
  run_sudo curl -fsSL "$gpg_url" -o "$key_path"
  run_sudo chmod a+r "$key_path"

  # 清理旧式 list，避免与 deb822 双源冲突
  if [[ -f /etc/apt/sources.list.d/docker.list ]]; then
    run_sudo rm -f /etc/apt/sources.list.d/docker.list
  fi

  run_sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: ${apt_uri}
Suites: ${codename}
Components: stable
Architectures: ${arch}
Signed-By: ${key_path}
EOF

  run_sudo apt-get update -y
}

install_packages() {
  log "安装 docker-ce / cli / containerd / buildx / compose 插件…"
  run_sudo apt-get install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin
}

write_daemon_mirrors() {
  local mirrors_csv="${1:-}"
  if [[ -z "$mirrors_csv" ]]; then
    warn "LWA_DOCKER_REGISTRY_MIRRORS=none：跳过 registry-mirrors 写入"
    return 0
  fi
  need_cmd python3
  log "合并写入 /etc/docker/daemon.json registry-mirrors…"
  run_sudo mkdir -p /etc/docker
  run_sudo env MIRRORS_CSV="$mirrors_csv" python3 - <<'PY'
import json, os, pathlib, shutil, tempfile

path = pathlib.Path("/etc/docker/daemon.json")
mirrors = [x.strip() for x in os.environ.get("MIRRORS_CSV", "").split(",") if x.strip()]

data = {}
if path.exists():
    try:
        text = path.read_text(encoding="utf-8") or "{}"
        data = json.loads(text)
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

fd, tmp = tempfile.mkstemp(prefix="daemon.", suffix=".json", dir=str(path.parent))
os.close(fd)
tmp_path = pathlib.Path(tmp)
tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp_path.chmod(0o644)
tmp_path.replace(path)
print(f"已更新 {path} → registry-mirrors={merged}", flush=True)
PY
}

start_docker() {
  if command -v systemctl >/dev/null 2>&1; then
    log "启用并启动 docker 服务…"
    run_sudo systemctl enable --now docker || run_sudo systemctl start docker || true
  else
    warn "无 systemctl，尝试 service docker start"
    run_sudo service docker start || true
  fi
}

add_user_to_docker_group() {
  # https://docs.docker.com/engine/install/linux-postinstall/
  local user="${SUDO_USER:-${USER:-}}"
  if [[ -z "$user" || "$user" == "root" ]]; then
    warn "当前为 root 或无法识别登录用户，跳过 usermod -aG docker"
    return 0
  fi
  if getent group docker >/dev/null 2>&1; then
    log "将用户 $user 加入 docker 组（需重新登录后免 sudo）…"
    run_sudo usermod -aG docker "$user" || warn "usermod 失败，可手动：sudo usermod -aG docker $user"
  fi
}

verify_install() {
  log "校验安装…"
  run_sudo docker version
  run_sudo docker compose version

  local server_raw engine_ver compose_raw compose_ver
  server_raw="$(run_sudo docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
  engine_ver="$(extract_semver "$server_raw")"
  [[ -n "$engine_ver" ]] || die "无法读取 Docker Engine 版本（daemon 是否已启动？）"
  version_ge "$engine_ver" "$MIN_DOCKER_VERSION" || die "Engine $engine_ver < 最低要求 $MIN_DOCKER_VERSION"

  compose_raw="$(run_sudo docker compose version --short 2>/dev/null || run_sudo docker compose version)"
  compose_ver="$(extract_semver "$compose_raw")"
  [[ -n "$compose_ver" ]] || die "无法读取 Compose 版本"
  version_ge "$compose_ver" "$MIN_COMPOSE_VERSION" || die "Compose $compose_ver < 最低要求 $MIN_COMPOSE_VERSION"

  if [[ "${LWA_SKIP_HELLO_WORLD:-0}" != "1" ]]; then
    log "运行 hello-world 验证（可设 LWA_SKIP_HELLO_WORLD=1 跳过）…"
    if ! run_sudo docker run --rm hello-world; then
      warn "hello-world 失败（常见于镜像加速未配置或网络受限）；Engine/Compose 版本已达标"
    fi
  fi

  log "完成：Docker Engine $engine_ver，Compose $compose_ver"
  warn "若尚未重新登录，请执行：newgrp docker   或注销后重登，再免 sudo 使用 docker"
}

main() {
  need_cmd curl
  need_cmd apt-get
  need_cmd dpkg

  local codename
  codename="$(detect_ubuntu)"
  log "检测到 Ubuntu（代号 $codename）"

  if already_good; then
    write_daemon_mirrors "$DEFAULT_REGISTRY_MIRRORS"
    if [[ -n "$DEFAULT_REGISTRY_MIRRORS" ]]; then
      start_docker
      run_sudo systemctl restart docker 2>/dev/null || true
    fi
    log "无需重装，退出"
    exit 0
  fi

  uninstall_conflicts
  setup_apt_repo "$codename"
  install_packages
  write_daemon_mirrors "$DEFAULT_REGISTRY_MIRRORS"
  start_docker
  if [[ -n "$DEFAULT_REGISTRY_MIRRORS" ]]; then
    run_sudo systemctl restart docker 2>/dev/null || true
  fi
  add_user_to_docker_group
  verify_install
}

main "$@"
