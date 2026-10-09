import json
import os
import ssl
from pathlib import Path
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from al1s.components.host.config import HostSettings

settings = HostSettings.model_validate_json(
    Path(os.environ["AL1S_COMPONENT_CONFIG"]).read_text(encoding="utf-8")
)
opener = build_opener(
    ProxyHandler({}),
    HTTPSHandler(context=ssl.create_default_context(cafile=settings.platform.ca_file)),
)
request = Request(
    "https://component-maintenance:8012/health",
    headers={"Authorization": "Bearer " + settings.platform.token.get_secret_value()},
)
with opener.open(request, timeout=5) as response:
    if json.loads(response.read(1024)) != {"status": "ok"}:
        raise SystemExit(1)
