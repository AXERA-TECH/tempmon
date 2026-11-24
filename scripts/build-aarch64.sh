#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(cat VERSION)"
TOOLCHAIN_URL="${ARM_TOOLCHAIN_URL:-https://gitlab.arm.com/api/v4/projects/tooling%2Fgnu-toolchains-for-arm/packages/generic/gnu-toolchain/13.3.rel1/arm-gnu-toolchain-13.3.rel1-x86_64-aarch64-none-linux-gnu.tar.xz}"
TOOLCHAIN_DIR="${ARM_TOOLCHAIN_DIR:-${HOME}/.cache/tempmon/arm-gnu-toolchain-13.3.rel1}"
CC_BIN="$TOOLCHAIN_DIR/bin/aarch64-none-linux-gnu-gcc"

if [[ ! -x "$CC_BIN" ]]; then
  echo "Downloading Arm GNU Toolchain 13.3.rel1..." >&2
  mkdir -p "$TOOLCHAIN_DIR"
  archive="$(mktemp "${RUNNER_TEMP:-/tmp}/arm-gnu-toolchain.XXXXXX.tar.xz")"
  curl -fL --retry 5 --retry-delay 2 --connect-timeout 30 \
    -o "$archive" "$TOOLCHAIN_URL"
  tar -xJf "$archive" -C "$TOOLCHAIN_DIR" --strip-components=1
fi

OUT_DIR="dist/tempmon-${VERSION}-linux-aarch64"
mkdir -p "$OUT_DIR"

"$CC_BIN" \
  -static -O2 -Wall -Wextra -Werror -pthread -s \
  -o "$OUT_DIR/tempmon" tempmon.c

cp README.md LICENSE "$OUT_DIR/"
printf '%s\n' "$VERSION" > "$OUT_DIR/VERSION"

cat > "$OUT_DIR/README.txt" <<EOF
tempmon ${VERSION} linux-aarch64
================================

This archive contains a statically linked aarch64 build of the tempmon board agent.

Quick start on the board:

    chmod +x tempmon
    ./tempmon 8080

Health check:

    curl http://127.0.0.1:8080/health

Systemd deployment and configuration are documented in README.md.
EOF

chmod 755 "$OUT_DIR/tempmon"

tar -czf "dist/tempmon-${VERSION}-linux-aarch64.tar.gz" -C "$OUT_DIR" .
(
  cd dist
  sha256sum "tempmon-${VERSION}-linux-aarch64.tar.gz" \
    > "tempmon-${VERSION}-linux-aarch64.tar.gz.sha256"
)

echo "Built:"
ls -lh "dist/tempmon-${VERSION}-linux-aarch64.tar.gz" \
       "dist/tempmon-${VERSION}-linux-aarch64.tar.gz.sha256"
file "$OUT_DIR/tempmon"
