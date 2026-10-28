import socket
import subprocess

status = subprocess.check_output([
    "supervisorctl", "-c", "/etc/al1s/storage-supervisord.conf", "status"
], timeout=3, text=True)
roles = {line.split()[0]: line.split()[1] for line in status.splitlines()}
if roles != {name: "RUNNING" for name in ("master", "volume", "filer", "s3")}:
    raise SystemExit(1)
address = socket.gethostbyname(socket.gethostname())
for port in (9333, 8080, 8888, 8333):
    with socket.create_connection((address, port), timeout=1):
        pass
