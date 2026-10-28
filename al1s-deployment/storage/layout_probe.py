import configparser
import json
from pathlib import Path

config = configparser.ConfigParser()
config.read("/etc/al1s/storage-supervisord.conf")
roles = ["master", "volume", "filer", "s3"]
for role in roles:
    section = config["program:" + role]
    if not section["command"].startswith("/usr/bin/weed " + role + " "):
        raise SystemExit(1)
    if not Path(section["directory"]).is_dir():
        raise SystemExit(1)
if not Path("/app/storage-entry.py").is_file():
    raise SystemExit(1)
print(json.dumps({"layout": "seaweed-four-process-v1", "roles": roles}))
