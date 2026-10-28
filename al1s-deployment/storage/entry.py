import json
import os
from pathlib import Path

config = {"identities": [{"name": "al1s-platform", "credentials": [{
    "accessKey": os.environ["S3_ACCESS_KEY"], "secretKey": os.environ["S3_SECRET_KEY"]
}], "actions": ["Admin", "Read", "Write"]}]}
path = Path("/tmp/al1s-s3.json")
path.write_text(json.dumps(config), encoding="utf-8")
path.chmod(0o600)
os.execvp("supervisord", ["supervisord", "-c", "/etc/al1s/storage-supervisord.conf"])
