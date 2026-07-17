#!/usr/bin/env bash
# LWA：在 Ubuntu（含 WSL）上安装 Caddy（满足 MIN_CADDY_VERSION，默认 ≥ 2.10.0）。
#
# 优先：官方 apt 仓库（https://caddyserver.com/docs/install#debian-ubuntu-raspbian）
# 备选：若 apt 失败，提示手动安装。
#
# 用法：bash install-caddy-linux.sh
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
MIN_CADDY_VERSION="${LWA_MIN_CADDY_VERSION:-2.10.0}"

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

already_good() {
  command -v caddy >/dev/null 2>&1 || return 1
  local raw ver
  raw="$(caddy version 2>/dev/null | head -n1 || true)"
  ver="$(extract_semver "$raw")"
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

install_via_cloudsmith() {
  # 官方文档：Debian/Ubuntu 使用 Caddy 的 cloudsmith apt 源
  log "按官方方式配置 Caddy apt 源并安装…"
  run_sudo apt-get update -y
  run_sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | run_sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | run_sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  run_sudo apt-get update -y
  run_sudo apt-get install -y caddy
}

verify() {
  local raw ver
  raw="$(caddy version 2>/dev/null | head -n1 || true)"
  ver="$(extract_semver "$raw")"
  [[ -n "$ver" ]] || die "无法读取 caddy version"
  version_ge "$ver" "$MIN_CADDY_VERSION" || die "Caddy $ver < $MIN_CADDY_VERSION"
  log "完成：Caddy $ver"
  warn "若启用发行版 caddy.service，请勿与 lwa gateway on 同时托管（争用 :2019）；推荐由 LWA 管理 Caddy。"
}

main() {
  need_cmd curl
  need_cmd apt-get
  detect_ubuntu
  if already_good; then exit 0; fi
  install_via_cloudsmith
  verify
}

main "$@"
