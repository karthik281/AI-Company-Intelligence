#!/usr/bin/env bash
# Build Lambda deployment zips.
#
#   ./build.sh            # x86_64 (Lambda default)
#   ./build.sh arm64      # if your functions use Graviton (arm64)
#
# Output: dist/orchestrator.zip, dist/research.zip
# Python version must match the Lambda runtime (3.14 per your logs).

set -euo pipefail

ARCH="${1:-x86_64}"
PYVER="${PYVER:-3.14}"

case "$ARCH" in
  x86_64) PLATFORM="manylinux2014_x86_64" ;;
  arm64)  PLATFORM="manylinux2014_aarch64" ;;
  *) echo "Architecture must be x86_64 or arm64"; exit 1 ;;
esac

HERE="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$HERE/dist"
BUILD="$(mktemp -d)"
rm -rf "$DIST" && mkdir -p "$DIST"

# Zips a directory's contents (not using the external `zip` binary, which
# isn't available on every build machine) with Python's stdlib zipfile.
zip_dir() {
  local src="$1" dst="$2"
  python3 -c '
import os, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    for root, _, files in os.walk(src):
        for name in files:
            full = os.path.join(root, name)
            zf.write(full, os.path.relpath(full, src))
' "$src" "$dst"
}

echo "Building orchestrator for $ARCH / Python $PYVER"
mkdir -p "$BUILD/orchestrator"
pip install --quiet \
  --target "$BUILD/orchestrator" \
  --platform "$PLATFORM" \
  --implementation cp \
  --python-version "$PYVER" \
  --only-binary=:all: \
  -r "$HERE/orchestrator/requirements.txt"
cp "$HERE/orchestrator/lambda_function.py" \
   "$HERE/orchestrator/pdf_report.py" "$BUILD/orchestrator/"
cp -r "$HERE/orchestrator/fonts" "$BUILD/orchestrator/"
find "$BUILD/orchestrator" -name "__pycache__" -type d -prune -exec rm -rf {} +
zip_dir "$BUILD/orchestrator" "$DIST/orchestrator.zip"

echo "Building research Lambda (no extra dependencies)"
mkdir -p "$BUILD/research"
cp "$HERE/research/lambda_function.py" "$BUILD/research/"
zip_dir "$BUILD/research" "$DIST/research.zip"

rm -rf "$BUILD"
ls -lh "$DIST"
