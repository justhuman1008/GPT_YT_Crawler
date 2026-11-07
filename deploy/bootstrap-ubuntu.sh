#!/usr/bin/env bash
set -euo pipefail
umask 077

# Run on a fresh Ubuntu instance after copying this project to /opt/yt-archive.
# Leaves the collection timer disabled until channels and authentication are ready.
if [[ ${EUID} -ne 0 ]]; then
  echo 'Run with sudo.' >&2
  exit 1
fi
cd /opt/yt-archive
test -f pyproject.toml
test -f config.toml
export DEBIAN_FRONTEND=noninteractive
if [[ -z $(swapon --show --noheadings) && ! -e /swapfile ]]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  printf '/swapfile none swap sw 0 0\n' >> /etc/fstab
fi
apt-get update
apt-get install -y --no-install-recommends python3-venv ffmpeg curl unzip ca-certificates

install -d -m 700 /var/tmp/yt-archive-bootstrap
curl --fail --location --proto '=https' --tlsv1.2 https://rclone.org/install.sh -o /var/tmp/yt-archive-bootstrap/rclone-install.sh
bash /var/tmp/yt-archive-bootstrap/rclone-install.sh
curl --fail --location --proto '=https' --tlsv1.2 https://deno.land/install.sh -o /var/tmp/yt-archive-bootstrap/deno-install.sh
DENO_INSTALL=/opt/yt-archive-tools/deno sh /var/tmp/yt-archive-bootstrap/deno-install.sh --no-modify-path
ln -sf /opt/yt-archive-tools/deno/bin/deno /usr/local/bin/deno
chmod 755 /opt/yt-archive-tools /opt/yt-archive-tools/deno /opt/yt-archive-tools/deno/bin
chmod 755 /opt/yt-archive-tools/deno/bin/deno

if ! id ytarchive >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /opt/yt-archive --shell /usr/sbin/nologin ytarchive
fi
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
chown -R root:ytarchive /opt/yt-archive
chmod -R g-w,o-rwx /opt/yt-archive
chmod -R g+rX /opt/yt-archive
chmod 750 /opt/yt-archive
install -d -m 700 -o ytarchive -g ytarchive data secrets
chown -R ytarchive:ytarchive data secrets
find data secrets -type d -exec chmod 700 {} +
find data secrets -type f -exec chmod 600 {} +
chmod 640 config.toml

install -m 644 deploy/yt-archive.service deploy/yt-archive.timer deploy/yt-archive-report.service deploy/yt-archive-report.timer deploy/yt-archive-control.service deploy/yt-archive-control.timer /etc/systemd/system/
systemctl daemon-reload
systemctl disable --now yt-archive.timer
systemctl disable --now yt-archive-control.timer
systemd-analyze verify /etc/systemd/system/yt-archive.service /etc/systemd/system/yt-archive.timer /etc/systemd/system/yt-archive-report.service /etc/systemd/system/yt-archive-report.timer
.venv/bin/python -m unittest discover -s tests -q
systemctl enable --now yt-archive-report.timer
echo 'Installation complete. Collection timer remains disabled.'
