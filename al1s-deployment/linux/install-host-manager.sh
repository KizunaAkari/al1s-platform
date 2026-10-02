#!/usr/bin/env bash
# Explicit local installation; never enables, starts or restarts the service.
set -euo pipefail
umask 077
if [[ $# != 2 ]]; then
  echo "Usage: sudo bash install-host-manager.sh /absolute/terminal.whl /absolute/wheelhouse" >&2
  exit 2
fi
[[ $EUID == 0 ]] || { echo "Root required" >&2; exit 1; }
wheel=$(realpath -e -- "$1")
wheelhouse=$(realpath -e -- "$2")
script_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
[[ -f $wheel && $wheel == *.whl && -d $wheelhouse ]] || exit 2
[[ -f $wheelhouse/requirements.lock && ! -L $wheelhouse/requirements.lock ]] || {
  echo "Include the reviewed ARM64 requirements.lock in the offline wheelhouse" >&2; exit 2;
}
[[ -x /usr/bin/docker && -x /usr/bin/systemctl ]] || {
  echo "Docker/systemctl missing" >&2; exit 1;
}
python3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12)'
if systemctl is-active --quiet al1s-host-manager.service; then
  echo "Explicitly stop the host manager after maintenance completes before installation" >&2
  exit 1
fi
for config in host-manager.env host-manager.crt host-manager.key host-manager.token; do
  [[ -f /etc/al1s/$config && ! -L /etc/al1s/$config ]] || {
    echo "Prepare /etc/al1s/$config first" >&2; exit 1;
  }
done
# EnvironmentFile is not shell code; never source it.
for secret in host-manager.env host-manager.key host-manager.token; do
  [[ $(stat -c '%u:%a' "/etc/al1s/$secret") == 0:600 ]] || {
    echo "Require root ownership and 0600: /etc/al1s/$secret" >&2; exit 1;
  }
done
[[ ! -L /opt/al1s-host-manager && ! -L /opt/al1s-host-manager/venv ]] || {
  echo "Refusing symlink installation target" >&2; exit 1;
}
install -d -m 0755 /opt/al1s-host-manager
python3.12 -m venv /opt/al1s-host-manager/venv
/opt/al1s-host-manager/venv/bin/python -m pip install \
  --no-index --find-links "$wheelhouse" --constraint "$wheelhouse/requirements.lock" \
  --upgrade --force-reinstall "$wheel"
/opt/al1s-host-manager/venv/bin/python -c \
  'from importlib.resources import files; assert files("al1s_terminal.host_management.migrations.versions").joinpath("host_0003.py").is_file()'
install -m 0644 "$script_dir/al1s-host-manager.service" /etc/systemd/system/al1s-host-manager.service
systemctl daemon-reload
echo "Installed, not started/enabled. Review TLS, firewall and target container before activation."
