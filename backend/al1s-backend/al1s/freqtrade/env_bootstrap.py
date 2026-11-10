import os
import sys

from al1s.freqtrade.client import control_key


def main():
    required = ("AL1S_FREQTRADE_PLATFORM_DATABASE_URL", "AL1S_FREQTRADE_NATIVE_DATABASE_URL")
    if any(not os.environ.get(name) for name in required):
        raise SystemExit(78)
    allowed = (*required, "PATH", "PYTHONPATH", "TZ", "SSL_CERT_FILE")
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env["AL1S_FREQTRADE_CONTROL_TOKEN"] = control_key(
        os.environ.get("AL1S_QUANT_CONTROL_TOKEN", "")
    )
    env.update(
        PYTHONUNBUFFERED="1",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        NUMEXPR_NUM_THREADS="1",
        HOME="/nonexistent",
    )
    os.execve(sys.executable, [sys.executable, "-m", "al1s.freqtrade.runtime"], env)


if __name__ == "__main__":
    main()
