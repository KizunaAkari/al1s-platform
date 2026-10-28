import json
import os
import subprocess
from urllib.request import ProxyHandler, Request, build_opener

subprocess.run(["python", "/app/component-healthcheck.py"], timeout=7, check=True)
request = Request("http://127.0.0.1:8011/health", headers={
    "Authorization": "Bearer " + os.environ["AL1S_BOT_CONTROL_TOKEN"]
})
with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
    if json.loads(response.read(1024)) != {"status": "ok"}:
        raise SystemExit(1)
