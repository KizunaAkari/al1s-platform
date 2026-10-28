import os
from pathlib import Path

Path("/tmp/al1s-ops").mkdir(mode=0o700, exist_ok=True)
os.execvp("supervisord", ["supervisord", "-c", "/etc/al1s/ops-supervisord.conf"])
