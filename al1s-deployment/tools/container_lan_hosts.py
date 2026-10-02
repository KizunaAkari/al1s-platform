"""Scoped /etc/hosts mapping, executed by the deployment operator, not the API."""
import ipaddress
import re
import sys
from pathlib import Path

PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))


def rewrite_hosts(original: str, hostname: str, address: str) -> str:
    hostname = hostname.lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.local", hostname):
        raise ValueError("invalid_local_hostname")
    if address:
        ip = ipaddress.IPv4Address(address)
        if not any(ip in network for network in PRIVATE_NETWORKS):
            raise ValueError("non_private_address")
    marker = "# al1s-lan-discovery:" + hostname
    lines = original.splitlines(keepends=True)
    kept = []
    for line in lines:
        if line.rstrip().endswith(marker):
            continue
        names = line.split("#", 1)[0].split()[1:]
        if hostname in [name.lower() for name in names]:
            raise ValueError("unmanaged_hostname_conflict")
        kept.append(line)
    result = "".join(kept)
    if address:
        if result and not result.endswith("\n"):
            result += "\n"
        result += f"{address}\t{hostname}\t{marker}\n"
    return result


def main() -> None:
    import fcntl
    import os

    hostname, address = sys.argv[1:]
    # Docker owns this mount; rename is not supported. Serialize our in-place writes.
    with Path("/etc/hosts").open("r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        original = stream.read(65537)
        if len(original) > 65536:
            raise ValueError("hosts_file_too_large")
        updated = rewrite_hosts(original, hostname, "" if address == "remove" else address)
        if original != updated:
            stream.seek(0)
            stream.write(updated)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
    print("lan_mapping_updated")


if __name__ == "__main__":
    main()
