#!/usr/bin/env bash
# LWA：在 macOS 上安装 Caddy（满足 MIN_CADDY_VERSION，默认 ≥ 2.10.0）。
#
# 优先：Homebrew `brew install caddy`
# 参考：https://caddyserver.com/docs/install
#
# 用法：bash install-caddy-macos.sh
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

install_brew() {
  need_cmd brew
  if brew list caddy >/dev/null 2>&1; then
    log "升级 caddy…"
    brew upgrade caddy || true
  else
    log "brew install caddy…"
    brew install caddy
  fi
}

verify() {
  local raw ver
  raw="$(caddy version 2>/dev/null | head -n1 || true)"
  ver="$(extract_semver "$raw")"
  [[ -n "$ver" ]] || die "无法读取 caddy version"
  version_ge "$ver" "$MIN_CADDY_VERSION" || die "Caddy $ver < $MIN_CADDY_VERSION"
  log "完成：Caddy $ver"
}

main() {
  [[ "$(uname -s)" == "Darwin" ]] || die "本脚本仅支持 macOS"
  if already_good; then exit 0; fi
  install_brew
  verify
}

main "$@"
