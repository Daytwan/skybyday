#!/usr/bin/env bash
# Install a release bundle on a Linux systemd host; leaves the web server alone.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo 'Run with sudo: sudo bash deploy/install-service.sh RELEASE_NAME' >&2
  exit 1
fi
release_name=${1:?Supply a new release name, for example demo-2026-11-01}
if [[ ! $release_name =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$ ]]; then
  echo 'Release names must contain only letters, digits, dots, underscores and dashes.' >&2
  exit 1
fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
release_dir=/opt/skybyday/releases/$release_name
if [[ -e $release_dir || -L $release_dir ]]; then
  echo "Release already exists: $release_dir; choose a new name." >&2
  exit 1
fi
if [[ ! -f $source_dir/frontend/dist/index.html ]]; then
  echo 'Use the release bundle, or build the frontend before installing.' >&2
  exit 1
fi
command -v curl >/dev/null
command -v systemctl >/dev/null
umask 022
mkdir -p /opt/skybyday/{bin,cache,python,releases}
if [[ ! -x /opt/skybyday/bin/uv ]]; then
  # Official uv installer, pinned to the version used to prepare this demo.
  curl --fail --silent --show-error --location https://astral.sh/uv/0.12.5/install.sh \
    -o /opt/skybyday/cache/uv-install.sh
  env UV_INSTALL_DIR=/opt/skybyday/bin UV_NO_MODIFY_PATH=1 \
    sh /opt/skybyday/cache/uv-install.sh
fi
id skybyday >/dev/null 2>&1 || \
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin skybyday
mkdir -p "$release_dir/backend" "$release_dir/frontend"
# Copy only deployment inputs, never local environment files or an existing venv.
cp -a "$source_dir/backend/src" "$release_dir/backend/"
cp "$source_dir/backend/pyproject.toml" "$source_dir/backend/uv.lock" \
  "$source_dir/backend/.python-version" "$release_dir/backend/"
cp -a "$source_dir/frontend/dist" "$release_dir/frontend/"
env UV_PYTHON_INSTALL_DIR=/opt/skybyday/python UV_CACHE_DIR=/opt/skybyday/cache \
  /opt/skybyday/bin/uv sync --project "$release_dir/backend" --locked --no-dev \
  --python 3.12 --managed-python

previous=$(readlink -f /opt/skybyday/current || true)
if [[ -n $previous && $previous != /opt/skybyday/current ]]; then
  printf '%s\n' "$previous" > /opt/skybyday/previous-release
fi
ln -s "$release_dir" /opt/skybyday/current.next
mv -Tf /opt/skybyday/current.next /opt/skybyday/current
if [[ -f /etc/systemd/system/skybyday.service ]]; then
  cp /etc/systemd/system/skybyday.service "$release_dir/previous-skybyday.service"
fi
install -m 0644 "$source_dir/deploy/skybyday.service" /etc/systemd/system/skybyday.service
systemctl daemon-reload
systemctl enable skybyday
systemctl restart skybyday
for attempt in {1..15}; do
  if curl --fail --silent http://127.0.0.1:8091/api/health; then
    printf '\nService installed. Configure the fly.donutmath.com virtual host and HTTPS next.\n'
    exit 0
  fi
  sleep 1
done
echo 'Health check failed. Inspect journalctl -u skybyday; previous release path is recorded.' >&2
exit 1
