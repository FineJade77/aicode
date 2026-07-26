#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${INSTALL_PYTHON:-python3}"
version="$(tr -d '[:space:]' < "$repo_root/VERSION")"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/aicode-install-e2e.XXXXXX")"
install_prefix="$test_root/prefix"
clean_home="$test_root/home"
state_home="$test_root/state"
workspace="$test_root/workspace"
binary="$install_prefix/bin/aicode"

cleanup() {
  if [[ -x "$binary" ]]; then
    AICODE_HOME="$state_home" "$binary" daemon stop >/dev/null 2>&1 || true
  fi
  if [[ -n "$test_root" && -d "$test_root" ]]; then
    rm -rf "$test_root"
  fi
}
trap cleanup EXIT

mkdir -p "$clean_home" "$state_home" "$workspace"
export HOME="$clean_home"
export AICODE_HOME="$state_home"
unset AICODE_RUNTIME_DIR AICODE_INSTALL_MANIFEST AICODE_RUNTIME_PYTHON

make -C "$repo_root" install \
  INSTALL_PREFIX="$install_prefix" \
  INSTALL_PYTHON="$python_bin" \
  INSTALL_SYSTEM_SITE_PACKAGES=1 \
  INSTALL_SKIP_DEPS=1

# Reinstalling the same version must be idempotent.
make -C "$repo_root" install \
  INSTALL_PREFIX="$install_prefix" \
  INSTALL_PYTHON="$python_bin" \
  INSTALL_SYSTEM_SITE_PACKAGES=1 \
  INSTALL_SKIP_DEPS=1 >/dev/null

test -x "$binary"
test -f "$install_prefix/lib/aicode/manifest.json"
test -f "$install_prefix/lib/aicode/$version/runtime/app/server/main.py"
test -x "$install_prefix/lib/aicode/$version/venv/bin/python"

# A failed staged install must leave the active binary, manifest and Runtime usable.
if "$python_bin" "$repo_root/scripts/install.py" \
  --source-root "$repo_root" \
  --prefix "$install_prefix" \
  --version "$version" \
  --cli "$binary" \
  --python "$test_root/missing-python" \
  --skip-deps >/dev/null 2>&1; then
  echo "expected staged install with missing Python to fail" >&2
  exit 1
fi
test -x "$binary"
test -f "$install_prefix/lib/aicode/manifest.json"
test -f "$install_prefix/lib/aicode/$version/runtime/app/server/main.py"

port="$(
  "$python_bin" -c \
    'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
)"
"$binary" config set runtime.url "http://127.0.0.1:$port" >/dev/null
"$binary" config set runtime.port "$port" >/dev/null

cd "$workspace"
"$binary" daemon start >/dev/null

status_output=""
for _attempt in {1..50}; do
  if status_output="$("$binary" daemon status 2>/dev/null)"; then
    break
  fi
  sleep 0.1
done

if [[ "$status_output" != *"status: ok"* || "$status_output" != *"version: $version"* ]]; then
  echo "installed daemon did not become ready" >&2
  echo "$status_output" >&2
  if [[ -f "$state_home/runtime.log" ]]; then
    sed -n '1,200p' "$state_home/runtime.log" >&2
  fi
  exit 1
fi

test -f "$state_home/runtime.pid"
test -f "$state_home/runtime.token"
"$binary" daemon stop >/dev/null
test ! -e "$state_home/runtime.pid"
test ! -e "$state_home/runtime.token"

echo "clean-home install/start/status/stop passed"
