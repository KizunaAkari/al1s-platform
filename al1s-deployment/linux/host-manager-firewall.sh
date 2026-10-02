#!/usr/bin/env bash
# Own only AL1S_HOST_MANAGER and its single INPUT jump; never flush host rules.
set -euo pipefail
exec 9>/run/al1s-host-manager-firewall.lock
flock -x 9
platform_name=${AL1S_PLATFORM_HOSTNAME:?set platform DNS hostname}
address=$(getent ahostsv4 "$platform_name" | awk '{print $1}' | sort -u)
python3 -c 'import ipaddress,sys; a=ipaddress.IPv4Address(sys.argv[1]); assert a.is_private and not a.is_loopback and not a.is_link_local' "$address"
# Restore commits the chain replacement atomically, including on DHCP changes.
iptables-restore --wait 5 --noflush <<EOF
*filter
:AL1S_HOST_MANAGER - [0:0]
-F AL1S_HOST_MANAGER
-A AL1S_HOST_MANAGER -s $address/32 -j ACCEPT
-A AL1S_HOST_MANAGER -j DROP
COMMIT
EOF
iptables --wait 5 -C INPUT -p tcp --dport 8771 -j AL1S_HOST_MANAGER 2>/dev/null ||
  iptables --wait 5 -I INPUT 1 -p tcp --dport 8771 -j AL1S_HOST_MANAGER
