#!/usr/bin/env bash
# LWA：在 Ubuntu（含 WSL）上安装 Caddy（满足 MIN_CADDY_VERSION，默认 ≥ 2.10.0）。
#
# 优先：官方 Cloudsmith apt 仓库
#   https://caddyserver.com/docs/install#debian-ubuntu-raspbian
# 备选：官方 GitHub Release 二进制（Cloudsmith 不可达、或 apt 仍落到 Ubuntu 旧包时）
#   https://github.com/caddyserver/caddy/releases
#
# 用法：bash install-caddy-linux.sh
#
# 环境变量：
#   LWA_MIN_CADDY_VERSION     最低版本（默认 2.10.0）
#   LWA_CADDY_RELEASE_VERSION GitHub 回退时下载的版本（默认 2.11.4）
#   LWA_CADDY_GITHUB_BASE     Release 下载根 URL（可换镜像；默认 github.com）
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
MIN_CADDY_VERSION="${LWA_MIN_CADDY_VERSION:-2.10.0}"
# GitHub 回退用的稳定版；勿用 MIN（门槛号未必有对应 release asset）
CADDY_RELEASE_VERSION="${LWA_CADDY_RELEASE_VERSION:-2.11.4}"
GITHUB_BASE="${LWA_CADDY_GITHUB_BASE:-https://github.com/caddyserver/caddy/releases/download}"

KEYRING_PATH="/usr/share/keyrings/caddy-stable-archive-keyring.gpg"
LIST_PATH="/etc/apt/sources.list.d/caddy-stable.list"

log()  { printf '==> [%s] %s\n' "$SCRIPT_NAME" "$*"; }
warn() { printf '⚠️  [%s] %s\n' "$SCRIPT_NAME" "$*" >&2; }
die()  { printf '❌ [%s] %s\n' "$SCRIPT_NAME" "$*" >&2; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"; }

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

run_sudo() {
  if [[ "$(id -u)" -eq 0 ]]; then "$@"
  else need_cmd sudo; sudo "$@"
  fi
}

caddy_installed_version() {
  command -v caddy >/dev/null 2>&1 || { echo ""; return 0; }
  local raw
  raw="$(caddy version 2>/dev/null | head -n1 || true)"
  extract_semver "$raw"
}

already_good() {
  local ver
  ver="$(caddy_installed_version)"
  [[ -n "$ver" ]] || return 1
  version_ge "$ver" "$MIN_CADDY_VERSION" || {
    warn "Caddy $ver < $MIN_CADDY_VERSION，将继续安装/升级"
    return 1
  }
  log "已满足：Caddy $ver（≥ $MIN_CADDY_VERSION）"
  return 0
}

detect_ubuntu() {
  [[ -f /etc/os-release ]] || die "仅支持 Ubuntu（含 WSL Ubuntu）"
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || die "当前 ID=${ID:-?}，本期仅支持 Ubuntu"
}

detect_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo "amd64" ;;
    aarch64|arm64) echo "arm64" ;;
    armv7l|armhf) echo "armv7" ;;
    *) die "不支持的架构: $(uname -m)" ;;
  esac
}

# 候选包是否来自 Cloudsmith，且版本 ≥ MIN
cloudsmith_candidate_ok() {
  local policy candidate ver
  policy="$(apt-cache policy caddy 2>/dev/null || true)"
  [[ -n "$policy" ]] || return 1
  echo "$policy" | grep -qiE 'cloudsmith\.io|dl\.cloudsmith' || return 1
  candidate="$(echo "$policy" | awk '/Candidate:/ {print $2; exit}')"
  [[ -n "$candidate" && "$candidate" != "(none)" ]] || return 1
  ver="$(extract_semver "$candidate")"
  [[ -n "$ver" ]] || return 1
  version_ge "$ver" "$MIN_CADDY_VERSION"
}

apt_policy_hint() {
  apt-cache policy caddy 2>/dev/null | head -n 20 || true
}

refresh_apt_with_retry() {
  local attempt
  for attempt in 1 2 3; do
    log "apt-get update（第 ${attempt}/3 次）…"
    if run_sudo apt-get update -y; then
      return 0
    fi
    warn "apt-get update 失败，稍后重试…"
    sleep $((attempt * 2))
  done
  return 1
}

disable_system_caddy_service() {
  # apt 包会启用 caddy.service，与 lwa gateway on 争用 :2019；由 LWA 托管时先停掉
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files caddy.service >/dev/null 2>&1; then
      log "禁用系统 caddy.service（避免与 lwa gateway 争用 :2019）…"
      run_sudo systemctl disable --now caddy.service 2>/dev/null || true
    fi
  fi
}

install_via_cloudsmith() {
  # 官方文档：Debian/Ubuntu 使用 Caddy 的 cloudsmith apt 源
  # 返回 0=成功装到达标版本；1=应回退 GitHub（网络/索引/旧包）
  log "按官方方式配置 Caddy apt 源并安装…"
  run_sudo apt-get update -y || warn "预更新 apt 失败，继续尝试…"
  run_sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg

  log "下载 Cloudsmith GPG key…"
  run_sudo rm -f "$KEYRING_PATH"
  # -S：静默模式下仍打印错误（旧版 -sLf 失败时几乎无输出，易被当成「脚本莫名退出」）
  if ! curl -1sSfL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | run_sudo gpg --dearmor -o "$KEYRING_PATH"; then
    warn "无法从 dl.cloudsmith.io 获取 GPG key（网络受限或暂时不可达）"
    return 1
  fi

  log "写入 Cloudsmith apt 源列表…"
  if ! curl -1sSfL 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      | run_sudo tee "$LIST_PATH" >/dev/null; then
    warn "无法获取 Cloudsmith debian.deb.txt"
    return 1
  fi
  # 官方文档要求：keyring / list 对 apt 可读
  run_sudo chmod o+r "$KEYRING_PATH" "$LIST_PATH" 2>/dev/null || true

  if ! refresh_apt_with_retry; then
    warn "添加 Cloudsmith 源后 apt-get update 仍失败"
    return 1
  fi

  if ! cloudsmith_candidate_ok; then
    warn "Cloudsmith 候选包未就绪或版本 < $MIN_CADDY_VERSION"
    warn "当前 apt-cache policy caddy："
    apt_policy_hint >&2
    # 再刷一次索引（偶发首次未拉全）
    refresh_apt_with_retry || true
    if ! cloudsmith_candidate_ok; then
      warn "二次刷新后仍无达标的 Cloudsmith 候选；避免 apt 静默装上 Ubuntu universe 旧包"
      return 1
    fi
  fi

  local candidate
  candidate="$(apt-cache policy caddy 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
  log "安装 Caddy（候选 $candidate）…"
  if ! run_sudo apt-get install -y caddy; then
    warn "apt-get install caddy 失败"
    return 1
  fi

  local ver
  ver="$(caddy_installed_version)"
  if [[ -z "$ver" ]] || ! version_ge "$ver" "$MIN_CADDY_VERSION"; then
    warn "apt 安装后版本为 ${ver:-unknown}，不满足 ≥ $MIN_CADDY_VERSION"
    warn "当前 apt-cache policy caddy："
    apt_policy_hint >&2
    # 尝试按 madison 中 cloudsmith 行强制钉版本
    local pin
    pin="$(apt-cache madison caddy 2>/dev/null | grep -i cloudsmith | head -1 | awk '{print $3}' || true)"
    if [[ -n "$pin" ]]; then
      log "尝试强制安装 caddy=$pin …"
      run_sudo apt-get install -y "caddy=$pin" || true
      ver="$(caddy_installed_version)"
    fi
    if [[ -z "$ver" ]] || ! version_ge "$ver" "$MIN_CADDY_VERSION"; then
      return 1
    fi
  fi

  disable_system_caddy_service
  return 0
}

install_via_github_release() {
  local arch ver url tmpdir tarball
  arch="$(detect_arch)"
  ver="$CADDY_RELEASE_VERSION"
  version_ge "$ver" "$MIN_CADDY_VERSION" || die "LWA_CADDY_RELEASE_VERSION=$ver < $MIN_CADDY_VERSION"

  url="${GITHUB_BASE}/v${ver}/caddy_${ver}_linux_${arch}.tar.gz"
  log "从 GitHub Release 安装 Caddy $ver（$arch）…"
  log "下载：$url"

  tmpdir="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmpdir'" RETURN
  tarball="$tmpdir/caddy.tgz"

  if ! curl -fsSL --retry 3 --retry-delay 2 -o "$tarball" "$url"; then
    warn "GitHub Release 下载失败。可设 LWA_CADDY_GITHUB_BASE 换镜像后重试。"
    return 1
  fi

  tar -xzf "$tarball" -C "$tmpdir" caddy
  run_sudo install -m 0755 "$tmpdir/caddy" /usr/local/bin/caddy

  # 若 PATH 上仍优先命中 /usr/bin/caddy（旧 apt 包），升级或替换
  if [[ -x /usr/bin/caddy ]]; then
    local bin_ver
    bin_ver="$(extract_semver "$(/usr/bin/caddy version 2>/dev/null | head -n1 || true)")"
    if [[ -z "$bin_ver" ]] || ! version_ge "$bin_ver" "$MIN_CADDY_VERSION"; then
      warn "/usr/bin/caddy 仍为旧版 ${bin_ver:-unknown}，一并覆盖为 $ver"
      run_sudo install -m 0755 "$tmpdir/caddy" /usr/bin/caddy
    fi
  fi

  disable_system_caddy_service
  hash -r 2>/dev/null || true
  return 0
}

verify() {
  local raw ver
  raw="$(caddy version 2>/dev/null | head -n1 || true)"
  ver="$(extract_semver "$raw")"
  [[ -n "$ver" ]] || die "无法读取 caddy version"
  if ! version_ge "$ver" "$MIN_CADDY_VERSION"; then
    warn "当前 apt-cache policy caddy："
    apt_policy_hint >&2
    die "Caddy $ver < $MIN_CADDY_VERSION。请勿使用 Ubuntu universe 旧包；确认 Cloudsmith 源可用后执行：sudo apt update && sudo apt install -y caddy；或重跑本脚本走 GitHub 回退。"
  fi
  log "完成：Caddy $ver"
  warn "若启用发行版 caddy.service，请勿与 lwa gateway on 同时托管（争用 :2019）；推荐由 LWA 管理 Caddy（脚本已尝试 disable）。"
}

main() {
  need_cmd curl
  need_cmd apt-get
  need_cmd tar
  detect_ubuntu
  if already_good; then exit 0; fi

  if install_via_cloudsmith; then
    verify
    exit 0
  fi

  warn "Cloudsmith apt 路径未装到达标版本，回退 GitHub Release…"
  install_via_github_release || die "Cloudsmith 与 GitHub 安装均失败。可手动：https://caddyserver.com/docs/install"
  verify
}

main "$@"
