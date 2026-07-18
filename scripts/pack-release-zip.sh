#!/usr/bin/env bash
# BUG-202：打包可 pip install -e 的源码发布 zip（必须含 pyproject.toml）。
#
# 用法（在仓库根目录）：
#   bash scripts/pack-release-zip.sh
#   bash scripts/pack-release-zip.sh /path/to/lwa-0.6.1-src.zip
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/归档.zip}"
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/lwa-pack.XXXXXX")"
cleanup() { rm -rf "$STAGING"; }
trap cleanup EXIT

cd "$ROOT"
test -f pyproject.toml || { echo "缺少 pyproject.toml，拒绝打包" >&2; exit 1; }
test -d src/local_webpage_access || { echo "缺少 src/local_webpage_access" >&2; exit 1; }

mkdir -p "$STAGING"
# 最小可安装集合：pyproject + README + 源码 + docs（含内置安装脚本的包数据）
cp -a pyproject.toml README.md LICENSE "$STAGING/" 2>/dev/null || cp -a pyproject.toml README.md "$STAGING/"
cp -a src "$STAGING/"
if [[ -d docs ]]; then
  cp -a docs "$STAGING/"
fi

# 校验关键文件
test -f "$STAGING/pyproject.toml"
test -d "$STAGING/src/local_webpage_access"
rg -q '\[project\.scripts\]' "$STAGING/pyproject.toml" || {
  echo "pyproject.toml 缺少 [project.scripts]" >&2
  exit 1
}

rm -f "$OUT"
(
  cd "$STAGING"
  # 排除缓存与 macOS 垃圾
  find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find . -name '.DS_Store' -delete 2>/dev/null || true
  zip -qr "$OUT" .
)

echo "已生成：$OUT"
# BUG-206：原 `unzip -l "$OUT" | head -30` 在 `set -o pipefail` 下，head 读满 30 行
# 即关闭管道，unzip 收到 SIGPIPE 退出码 141，使本脚本整体返回 141、被发布流水线
# 误判为失败。改为先把完整列表重定向到临时文件（unzip 正常写完，无管道），再 head/rg。
_listing="$STAGING/.release-listing.txt"
unzip -l "$OUT" > "$_listing"
head -30 "$_listing"
echo "..."
rg -n 'pyproject\.toml|src/local_webpage_access/__init__' "$_listing" || true
