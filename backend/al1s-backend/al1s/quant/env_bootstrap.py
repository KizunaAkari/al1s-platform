"""Drop inherited QQ secrets before starting the low-privilege quant process."""

import os
import sys


def main() -> None:
    allowed = (
        "AL1S_QUANT_CONTROL_TOKEN",
        "AL1S_QUANT_DATABASE_URL",
        "AL1S_QUANT_CREDENTIAL_KEY",
        "PATH",
        "PYTHONPATH",
        "TZ",
        "SSL_CERT_FILE",
    )
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        env["AL1S_QUANT_EASTMONEY_PROXY"] = proxy
    if len(env.get("AL1S_QUANT_CONTROL_TOKEN", "").encode()) < 32 or not env.get(
        "AL1S_QUANT_DATABASE_URL"
    ):
        raise SystemExit(78)
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "POLARS_MAX_THREADS": "1",
            "RAYON_NUM_THREADS": "1",
            "HOME": "/nonexistent",
        }
    )
    os.execve(sys.executable, [sys.executable, "-m", "al1s.quant.socket_runtime"], env)


if __name__ == "__main__":
    main()
