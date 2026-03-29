#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/out/pypdfium2_wheels}"
PYPDFIUM2_REF="${PYPDFIUM2_REF:-5.6.0}"
PYPDFIUM2_REPO="${PYPDFIUM2_REPO:-https://github.com/pypdfium2-team/pypdfium2.git}"
PYTHON_BIN="${PYTHON_BIN:-/opt/python/cp311-cp311/bin/python}"
PDFIUM_PLATFORM_SPEC="${PDFIUM_PLATFORM_SPEC:-sourcebuild-native:main}"
BUILD_PARAMS="${BUILD_PARAMS:---vendor all --no-vendor libc++}"

usage() {
  cat <<'EOF'
Usage: tools/build_pypdfium2_wheels.sh [linux_x64] [linux_arm64]

Build custom Linux pypdfium2 wheels that bundle the current PDFium checkout.

Environment variables:
  OUT_DIR         Output directory. Defaults to out/pypdfium2_wheels.
  PYPDFIUM2_REF   Upstream pypdfium2 ref to package. Defaults to 5.6.0.
  PYPDFIUM2_REPO  Upstream pypdfium2 repository. Defaults to the official repo.
  PYTHON_BIN      Python interpreter path inside the manylinux container.
  PDFIUM_PLATFORM_SPEC
                  PDFIUM_PLATFORM value for pypdfium2. Defaults to sourcebuild-native:main.
  BUILD_PARAMS    Arguments forwarded to pypdfium2's sourcebuild-native flow.
EOF
}

ensure_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
}

docker_platform_for() {
  case "$1" in
    linux_x64) echo "linux/amd64" ;;
    linux_arm64) echo "linux/arm64" ;;
    *) return 1 ;;
  esac
}

container_image_for() {
  case "$1" in
    linux_x64) echo "quay.io/pypa/manylinux_2_28_x86_64" ;;
    linux_arm64) echo "quay.io/pypa/manylinux_2_28_aarch64" ;;
    *) return 1 ;;
  esac
}

gn_platform_for() {
  case "$1" in
    linux_x64) echo "linux-amd64" ;;
    linux_arm64) echo "linux-arm64" ;;
    *) return 1 ;;
  esac
}

wheel_tag_for() {
  case "$1" in
    linux_x64) echo "manylinux_2_17_x86_64.manylinux2014_x86_64" ;;
    linux_arm64) echo "manylinux_2_17_aarch64.manylinux2014_aarch64" ;;
    *) return 1 ;;
  esac
}

patch_staged_pdfium() {
  python3 - <<'PY'
import re
from pathlib import Path

root = Path("/work/pypdfium2/sbuild/native/pdfium")

build_gn = root / "BUILD.gn"
text = build_gn.read_text()
old = 'component("pdfium")'
new = 'shared_library("pdfium")'
if old not in text:
    raise SystemExit(f"Expected {old!r} in {build_gn}")
build_gn.write_text(text.replace(old, new, 1))

fpdfview = root / "public" / "fpdfview.h"
text = fpdfview.read_text()
old = "#if defined(COMPONENT_BUILD)"
new = "#if 1  // defined(COMPONENT_BUILD)"
if old not in text:
    raise SystemExit(f"Expected {old!r} in {fpdfview}")
fpdfview.write_text(text.replace(old, new, 1))

for header in sorted((root / "public" / "cpp").glob("*.h")):
    text = header.read_text()
    patched = re.sub(r'"public/(.+)"', r'"../\1"', text)
    header.write_text(patched)
PY
}

patch_pypdfium2_source() {
  python3 - <<'PY'
from pathlib import Path

build_native = Path("/work/pypdfium2/setupsrc/build_native.py")
text = build_native.read_text()
replacements = {
    '    "pdf_is_standalone": True,\n':
        '    "pdf_is_standalone": True,\n'
        '    "build_with_chromium": False,\n',
    '        git_apply_patch(PatchDir/"legacy_gn.patch", cwd=PDFIUM_DIR_build)\n':
        '        siso_gni = PDFIUM_DIR_build/"toolchain"/"siso.gni"\n'
        '        siso_text = siso_gni.read_text()\n'
        '        for marker in ("_is_google_corp_machine = false", "_is_ninja_used = path_exists("):\n'
        '            if marker in siso_text:\n'
        '                start = siso_text.index(marker)\n'
        '                break\n'
        '        else:\n'
        '            raise ValueError("Could not find siso.gni marker to rewrite")\n'
        '        end = siso_text.index("declare_args() {")\n'
        '        siso_gni.write_text(siso_text[:start] + "# XXX(pypdfium2) patched away broken calls\\n\\n" + siso_text[end:])\n',
    '            git_apply_patch(PatchDir/"ffp_contract.patch", cwd=PDFIUM_DIR_build)\n':
        '            pass  # Newer Chromium build configs already handle ffp-contract settings for GCC.\n',
}
for old, new in replacements.items():
    if old not in text:
        raise SystemExit(f"Expected patch hook in {build_native}: {old.strip()}")
    text = text.replace(old, new, 1)
build_native.write_text(text)
PY
}

build_target() {
  local target="$1"
  local docker_platform
  local container_image
  local wheel_tag
  local gn_platform

  if ! docker_platform="$(docker_platform_for "$target")" \
    || ! container_image="$(container_image_for "$target")" \
    || ! wheel_tag="$(wheel_tag_for "$target")" \
    || ! gn_platform="$(gn_platform_for "$target")"; then
    echo "Unsupported target: $target" >&2
    exit 1
  fi

  mkdir -p "$OUT_DIR/$target"

  docker run --rm \
    --platform "$docker_platform" \
    -e PYPDFIUM2_REF="$PYPDFIUM2_REF" \
    -e PYPDFIUM2_REPO="$PYPDFIUM2_REPO" \
    -e PYTHON_BIN="$PYTHON_BIN" \
    -e PDFIUM_PLATFORM_SPEC="$PDFIUM_PLATFORM_SPEC" \
    -e BUILD_PARAMS="$BUILD_PARAMS" \
    -e TARGET="$target" \
    -e WHEEL_TAG="$wheel_tag" \
    -e GN_PLATFORM="$gn_platform" \
    -v "$ROOT_DIR:/pdfium-src:ro" \
    -v "$OUT_DIR:/out" \
    "$container_image" \
    /bin/bash -lc '
      set -euo pipefail

      export PIP_DISABLE_PIP_VERSION_CHECK=1
      export PYTHONDONTWRITEBYTECODE=1

      dnf clean all
      dnf makecache --refresh
      dnf -y install git ninja-build gcc gcc-c++ curl unzip tar gzip findutils
      dnf -y install gn || true
      if ! command -v gn >/dev/null 2>&1; then
        curl -fsSL -o /tmp/gn.zip "https://chrome-infra-packages.appspot.com/dl/gn/gn/$GN_PLATFORM/+/latest"
        unzip -j /tmp/gn.zip gn -d /usr/local/bin
        chmod +x /usr/local/bin/gn
      fi

      rm -rf /work
      mkdir -p /work/pypdfium2
      git clone --depth 1 --branch "$PYPDFIUM2_REF" "$PYPDFIUM2_REPO" /work/pypdfium2

      mkdir -p /work/pypdfium2/sbuild/native/pdfium
      tar --exclude=.git --exclude=out --exclude=.DS_Store -C /pdfium-src -cf - . \
        | tar -C /work/pypdfium2/sbuild/native/pdfium -xf -

      '"$(declare -f patch_staged_pdfium)"'
      '"$(declare -f patch_pypdfium2_source)"'
      patch_staged_pdfium
      patch_pypdfium2_source

      "$PYTHON_BIN" -m pip install --no-cache-dir -U pip setuptools packaging wheel build
      "$PYTHON_BIN" -m pip install --no-cache-dir "ctypesgen @ git+https://github.com/pypdfium2-team/ctypesgen@pypdfium2"

      cd /work/pypdfium2
      export PDFIUM_PLATFORM="$PDFIUM_PLATFORM_SPEC"
      export CROSS_TAG="$WHEEL_TAG"
      "$PYTHON_BIN" -m build --wheel --no-isolation --outdir "/out/$TARGET"
    '
}

main() {
  ensure_cmd docker

  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  local -a targets=("$@")
  if [[ ${#targets[@]} -eq 0 ]]; then
    targets=(linux_x64 linux_arm64)
  fi

  local target
  for target in "${targets[@]}"; do
    echo "==> Building $target"
    build_target "$target"
  done

  echo
  echo "Wheels written to:"
  printf '  %s\n' "$OUT_DIR"
}

main "$@"
