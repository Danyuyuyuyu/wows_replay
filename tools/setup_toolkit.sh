#!/usr/bin/env bash
# 在 Linux 上下载并解压 wows-toolkit 的命令行工具。
#
#   bash zhenxun/plugins/wows_replay/tools/setup_toolkit.sh
#   WOWS_TOOLKIT_VERSION=v1.0.2-beta2 bash tools/setup_toolkit.sh
#
# 需要 curl 与 unzip。走的代理直接读环境变量 HTTPS_PROXY / HTTP_PROXY。
set -euo pipefail

VERSION=$(printenv WOWS_TOOLKIT_VERSION || true)
if [ -z "$VERSION" ]; then
    VERSION=v1.0.1
fi
case "$VERSION" in
    v[0-9]*) ;;
    *) echo "无效版本标签: $VERSION" >&2; exit 2 ;;
esac
case "$VERSION" in
    *[!A-Za-z0-9._-]*) echo "无效版本标签: $VERSION" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
DEST="$ROOT/wows_toolkit"
BIN="$DEST/bin"
ZIP_NAME="wows_toolkit_tools_"$VERSION"_linux64.zip"
ZIP="$DEST/$ZIP_NAME"
URL="https://github.com/landaire/wows-toolkit/releases/download/$VERSION/$ZIP_NAME"

echo "仓库根目录 : $ROOT"
echo "下载        : $URL"

mkdir -p "$DEST"
TMP_ZIP="$DEST/.$ZIP_NAME.partial.$$"
TMP_BIN=""
BACKUP="$DEST/.bin.backup.$$"
MARKER_TMP="$DEST/.installed_version.partial.$$"
MOVED_OLD=0
PROMOTED=0
COMMITTED=0

cleanup() {
    status=$?
    trap - EXIT INT TERM
    if [ "$COMMITTED" -ne 1 ]; then
        if [ "$PROMOTED" -eq 1 ] && [ -d "$BIN" ]; then
            rm -rf "$BIN"
        fi
        if [ "$MOVED_OLD" -eq 1 ] && [ -d "$BACKUP" ]; then
            mv "$BACKUP" "$BIN"
        fi
    fi
    if [ -n "$TMP_BIN" ] && [ -d "$TMP_BIN" ]; then
        rm -rf "$TMP_BIN"
    fi
    rm -f "$TMP_ZIP" "$MARKER_TMP"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

SOURCE_ZIP="$ZIP"
if [ -f "$ZIP" ] && unzip -tq "$ZIP" >/dev/null 2>&1; then
    echo "已存在且校验通过的压缩包，跳过下载"
else
    rm -f "$ZIP"
    curl -fL --retry 3 --retry-delay 2 -o "$TMP_ZIP" "$URL"
    unzip -tq "$TMP_ZIP" >/dev/null
    SOURCE_ZIP="$TMP_ZIP"
fi

if ! unzip -Z1 "$SOURCE_ZIP" | grep -Fxq "minimap_renderer"; then
    echo "压缩包缺少 minimap_renderer" >&2
    exit 1
fi
if ! unzip -Z1 "$SOURCE_ZIP" | grep -Fxq "replayshark"; then
    echo "压缩包缺少 replayshark" >&2
    exit 1
fi

TMP_BIN="$(mktemp -d "$DEST/.bin.partial.XXXXXX")"
unzip -o -q "$SOURCE_ZIP" -d "$TMP_BIN"
if [ ! -s "$TMP_BIN/minimap_renderer" ] || [ ! -s "$TMP_BIN/replayshark" ]; then
    echo "解压后的必需工具缺失或为空" >&2
    exit 1
fi
chmod +x "$TMP_BIN/minimap_renderer" "$TMP_BIN/replayshark"
for tool in minimap_renderer_cpu wows-data-mgr wowsunpack; do
    if [ -f "$TMP_BIN/$tool" ]; then
        chmod +x "$TMP_BIN/$tool"
    fi
done

if [ "$SOURCE_ZIP" = "$TMP_ZIP" ]; then
    mv "$TMP_ZIP" "$ZIP"
fi
if [ -d "$BIN" ]; then
    mv "$BIN" "$BACKUP"
    MOVED_OLD=1
fi
mv "$TMP_BIN" "$BIN"
TMP_BIN=""
PROMOTED=1
printf '%s\n' "$VERSION" > "$MARKER_TMP"
mv "$MARKER_TMP" "$DEST/.installed_version"
COMMITTED=1
if [ "$MOVED_OLD" -eq 1 ]; then
    rm -rf "$BACKUP"
fi

echo
echo "已安装到 $BIN:"
ls -1 "$BIN"
echo
echo "系统 glibc: $(ldd --version 2>/dev/null | head -n 1 || echo '未知')"
echo
echo "渲染还需要 Vulkan（无显卡时装软件实现）："
echo "  sudo apt install -y libvulkan1 libstdc++6 libgcc-s1 mesa-vulkan-drivers"
echo
echo "验证编码器："
echo "  $BIN/minimap_renderer --check-encoder"
