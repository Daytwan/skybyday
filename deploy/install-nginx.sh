#!/usr/bin/env bash
# Complete installation on the existing Nginx/systemd server.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
  echo 'Run with sudo: sudo bash deploy/install-nginx.sh RELEASE_NAME' >&2
  exit 1
fi
release_name=${1:?Supply a new release name}
if [[ ! $release_name =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$ ]]; then
  echo 'Invalid release name.' >&2
  exit 1
fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
command -v nginx >/dev/null
command -v certbot >/dev/null
getent ahostsv4 fly.donutmath.com >/dev/null || {
  echo 'Create the fly.donutmath.com DNS record before continuing.' >&2
  exit 1
}
nginx -t
backup_dir=/opt/skybyday/backups/$release_name
mkdir -p "$backup_dir"
chmod 700 /opt/skybyday/backups "$backup_dir"
if [[ ! -f $backup_dir/nginx-before.tar.gz ]]; then
  tar -C /etc/nginx -czf "$backup_dir/nginx-before.tar.gz" \
    nginx.conf sites-available sites-enabled
fi
if [[ ! -d /opt/skybyday/releases/$release_name ]]; then
  bash "$source_dir/deploy/install-service.sh" "$release_name"
elif [[ $(readlink -f /opt/skybyday/current) != /opt/skybyday/releases/$release_name ]]; then
  echo 'Release directory exists but is not active. Inspect it before retrying.' >&2
  exit 1
fi
new_site=false
if [[ ! -e /etc/nginx/sites-available/skybyday ]]; then
  install -m 0644 "$source_dir/deploy/nginx.conf" /etc/nginx/sites-available/skybyday
  new_site=true
fi
if [[ ! -e /etc/nginx/sites-enabled/skybyday ]]; then
  ln -s /etc/nginx/sites-available/skybyday /etc/nginx/sites-enabled/skybyday
fi
if ! nginx -t; then
  if [[ $new_site == true ]]; then
    rm /etc/nginx/sites-enabled/skybyday /etc/nginx/sites-available/skybyday
  fi
  echo 'Configuration check failed. Nginx has not been reloaded.' >&2
  exit 1
fi
systemctl reload nginx
# Reuse the existing Certbot account; this host already serves an HTTPS website.
certbot --nginx --non-interactive --redirect --keep-until-expiring -d fly.donutmath.com
nginx -t
curl --fail --silent --show-error --resolve fly.donutmath.com:443:127.0.0.1 \
  https://fly.donutmath.com/api/health
printf '\nSkybyDay is installed at https://fly.donutmath.com\n'
printf 'Previous Nginx configuration: %s/nginx-before.tar.gz\n' "$backup_dir"
