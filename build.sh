#!/usr/bin/env bash
set -euo pipefail

PACKAGE_NAME="topologis-exporter"
ZIP_NAME="${PACKAGE_NAME}.zip"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
TMP_DIR="$(mktemp -d)"
PACKAGE_DIR="${TMP_DIR}/${PACKAGE_NAME}"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

mkdir -p "${PACKAGE_DIR}" "${DIST_DIR}"

copy_file() {
  install -m 0644 "${ROOT_DIR}/$1" "${PACKAGE_DIR}/$1"
}

copy_dir() {
  mkdir -p "${PACKAGE_DIR}/$1"
  cp -R "${ROOT_DIR}/$1/." "${PACKAGE_DIR}/$1/"
}

copy_file "__init__.py"
copy_file "compat.py"
copy_file "topologis_plugin.py"
copy_file "metadata.txt"
copy_file "README.md"
copy_file "LICENSE.txt"
copy_file "TRADEMARKS.md"

copy_dir "core"
copy_dir "gui"
copy_dir "resources"

find "${PACKAGE_DIR}" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "${PACKAGE_DIR}" -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

rm -f "${DIST_DIR}/${ZIP_NAME}"

(
  cd "${TMP_DIR}"
  zip -qr "${DIST_DIR}/${ZIP_NAME}" "${PACKAGE_NAME}"
)

echo "Created ${DIST_DIR}/${ZIP_NAME}"
