#!/usr/bin/env bash
# LWA：在 Ubuntu 22.04+ / Debian 12+（含 WSL）上安装 Docker Engine + Compose 插件。
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
      https://docs.docker.com/engine/install/debian/
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

# BUG-203：宿主 Python 3.13 与 python3-apt（仅提供 cpython-312 .so）不匹配时，
# apt 的 command-not-found 钩子会在 apt-get update 阶段 import apt_pkg 失败。
check_apt_pkg() {
  if ! command -v python3 >/dev/null 2>&1; then
    return 0
  fi
  if python3 -c "import apt_pkg" 2>/dev/null; then
    return 0
  fi
  warn "当前 python3 无法 import apt_pkg（常见于 Python 3.13 + 仅含 3.12 的 python3-apt）。"
  warn "将设置 APT::Update::Post-Invoke-Success 跳过 command-not-found 钩子后继续。"
  export LWA_APT_PKG_BROKEN=1
  # 尽量让本会话 apt 跳过会炸的钩子（不改系统配置文件，避免越权写盘失败）
  export APT_CONFIG="${APT_CONFIG:-}"
}

apt_get() {
  # 包装 apt-get：apt_pkg 损坏时用 -o 关掉 Update Post-Invoke hooks
  if [[ "${LWA_APT_PKG_BROKEN:-0}" == "1" ]]; then
    run_sudo apt-get \
      -o "APT::Update::Post-Invoke-Success::=" \
      -o "APT::Update::Post-Invoke::=" \
      "$@"
  else
    run_sudo apt-get "$@"
  fi
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

# 输出两行：distro_family（ubuntu|debian）与 apt suite/codename。
# 严禁把 Debian 伪装成 Ubuntu 代号去拉 /linux/ubuntu 源。
detect_debian_family() {
  [[ -f /etc/os-release ]] || die "未找到 /etc/os-release，本脚本仅支持 Ubuntu 22.04 LTS+ / Debian 12+ Stable（含 WSL）"
  # shellcheck disable=SC1091
  . /etc/os-release
  local id="${ID:-}"
  local ver="${VERSION_ID:-}"
  local codename="${VERSION_CODENAME:-}"
  case "$id" in
    ubuntu)
      codename="${UBUNTU_CODENAME:-$codename}"
      [[ -n "$codename" ]] || die "无法解析 Ubuntu 代号（UBUNTU_CODENAME / VERSION_CODENAME）"
      # 仅 Ubuntu LTS：偶数年 .04，且 ≥ 22.04（BUG-261）
      local major="${ver%%.*}"
      local minor="0"
      if [[ "$ver" == *.* ]]; then
        minor="${ver#*.}"
        minor="${minor%%.*}"
      fi
      if [[ -z "$ver" ]] || [[ "$major" -lt 22 ]] || [[ $((major % 2)) -ne 0 ]] || [[ "$minor" -ne 4 ]]; then
        die "Ubuntu ${ver:-unknown}（${codename}）不是正式支持的 LTS（需 22.04/24.04/26.04）"
      fi
      # 未纳入矩阵的未来 LTS（如 28.04）
      if [[ "$major" -gt 26 ]]; then
        die "Ubuntu $ver 尚未纳入正式支持矩阵（当前 22.04/24.04/26.04）"
      fi
      # 版本/代号配对（须与 SUPPORTED_UBUNTU_LTS 一致）
      if [[ "$ver" == 22.04* && "$codename" != "jammy" ]]; then
        die "Ubuntu 版本/代号不匹配：VERSION_ID=$ver 应对 jammy，实际为 ${codename}"
      fi
      if [[ "$ver" == 24.04* && "$codename" != "noble" ]]; then
        die "Ubuntu 版本/代号不匹配：VERSION_ID=$ver 应对 noble，实际为 ${codename}"
      fi
      if [[ "$ver" == 26.04* && "$codename" != "resolute" ]]; then
        die "Ubuntu 版本/代号不匹配：VERSION_ID=$ver 应对 resolute，实际为 ${codename}"
      fi
      case "$codename" in
        # 须与 platform_support.SUPPORTED_UBUNTU_LTS 保持同步
        jammy|noble|resolute) ;;
        *)
          die "Ubuntu 代号 '${codename}' 不在 LTS 允许列表（jammy/noble/resolute；见 SUPPORTED_UBUNTU_LTS）"
          ;;
      esac
      printf 'ubuntu\n%s\n' "$codename"
      ;;
    debian)
      [[ -n "$codename" ]] || die "无法解析 Debian 代号（VERSION_CODENAME）"
      case "$codename" in
        sid|unstable|testing|rc-buggy)
          die "Debian ${codename} 不是 Stable；仅支持 bookworm/trixie（12/13）"
          ;;
      esac
      local major="${ver%%.*}"
      if [[ -z "$ver" ]] || [[ "$major" -lt 12 ]]; then
        die "Debian ${ver:-unknown} 低于最低要求 12（Bookworm）"
      fi
      case "$codename" in
        # 须与 platform_support.SUPPORTED_DEBIAN_STABLE 保持同步
        bookworm|trixie) ;;
        *)
          die "Debian 代号 '${codename}' 不在 Stable 允许列表（bookworm/trixie；见 SUPPORTED_DEBIAN_STABLE）"
          ;;
      esac
      # 拒绝未知未来大版本（如 99）假绿；须与 SUPPORTED_DEBIAN_STABLE 键一致
      if [[ "$major" -gt 13 ]]; then
        die "Debian $ver 尚未纳入正式支持的 Stable 矩阵（当前 12/13）"
      fi
      # 版本与代号配对（12↔bookworm、13↔trixie）
      if [[ "$major" -eq 12 && "$codename" != "bookworm" ]]; then
        die "Debian 版本/代号不匹配：VERSION_ID=$ver 应对 bookworm，实际为 ${codename}"
      fi
      if [[ "$major" -eq 13 && "$codename" != "trixie" ]]; then
        die "Debian 版本/代号不匹配：VERSION_ID=$ver 应对 trixie，实际为 ${codename}"
      fi
      printf 'debian\n%s\n' "$codename"
      ;;
    *)
      die "当前发行版 ID=${id:-unknown}。仅支持 Ubuntu 22.04 LTS+ / Debian 12+ Stable；见 https://docs.docker.com/engine/install/"
      ;;
  esac
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
    apt_get remove -y $pkgs || true
  else
    log "未发现冲突包，跳过"
  fi
}

setup_apt_repo() {
  # family=ubuntu|debian；严禁 Debian 使用 /linux/ubuntu
  # https://docs.docker.com/engine/install/ubuntu/
  # https://docs.docker.com/engine/install/debian/
  local family="$1"
  local codename="$2"
  local arch apt_uri gpg_url key_path
  arch="$(dpkg --print-architecture)"

  if [[ "$family" != "ubuntu" && "$family" != "debian" ]]; then
    die "内部错误：未知 distro family=$family"
  fi

  if [[ "$USE_OFFICIAL" -eq 1 ]]; then
    apt_uri="${OFFICIAL_APT_ROOT}/linux/${family}"
    gpg_url="${OFFICIAL_APT_ROOT}/linux/${family}/gpg"
    log "配置官方 apt 仓库：$apt_uri"
  else
    apt_uri="${LWA_DOCKER_APT_MIRROR:-$DEFAULT_APT_MIRROR}/linux/${family}"
    gpg_url="${LWA_DOCKER_APT_MIRROR:-$DEFAULT_APT_MIRROR}/linux/${family}/gpg"
    log "配置阿里云 Docker CE apt 仓库：$apt_uri"
  fi

  log "安装 ca-certificates / curl…"
  apt_get update -y
  apt_get install -y ca-certificates curl

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

  apt_get update -y
}

install_packages() {
  log "安装 docker-ce / cli / containerd / buildx / compose 插件…"
  apt_get install -y \
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
  warn "若已运行 lwa manager/daemon：须执行 lwa manager off && lwa manager on 与 lwa daemon off && lwa daemon on"
  warn "（或 systemctl --user restart lwa-manager.service lwa-daemon.service），否则后台进程仍无 docker 组、管理页会误标容器 stopped"
}

main() {
  need_cmd curl
  need_cmd apt-get
  need_cmd dpkg
  check_apt_pkg

  local family codename
  {
    read -r family
    read -r codename
  } < <(detect_debian_family)
  log "检测到 ${family}（代号 $codename）"

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
  setup_apt_repo "$family" "$codename"
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
