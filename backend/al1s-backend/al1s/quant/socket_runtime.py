"""Bind a single-owner Unix socket without exposing a network port."""

import os
import socket
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def secure_socket(directory: Path) -> Iterator[socket.socket]:
    import fcntl

    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("quant_socket_directory_unsafe")
    lock = os.open(directory / "control.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        lock_info = os.fstat(lock)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid():
            raise ValueError("quant_socket_lock_unsafe")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / "control.sock"
        try:
            old = path.lstat()
        except FileNotFoundError:
            old = None
        if old is not None:
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.geteuid():
                raise ValueError("quant_unsafe_socket")
            path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
            os.chmod(path, 0o600)
            identity = path.lstat()
            sock.listen(128)
            sock.set_inheritable(False)
            yield sock
        finally:
            sock.close()
            try:
                current = path.lstat()
                if (
                    "identity" in locals()
                    and current.st_ino == identity.st_ino
                    and current.st_dev == identity.st_dev
                    and stat.S_ISSOCK(current.st_mode)
                ):
                    path.unlink()
            except FileNotFoundError:
                pass
    finally:
        os.close(lock)


def run() -> None:
    from importlib.metadata import version

    import uvicorn
    from akquant import Engine
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from al1s.adapters.postgres.quant_accounts import QuantAccounts
    from al1s.adapters.postgres.quant_backtests import QuantBacktests
    from al1s.adapters.postgres.quant_corporate_sync import QuantCorporateSync
    from al1s.adapters.postgres.quant_credentials import QuantCredentials
    from al1s.adapters.postgres.quant_entitlements import QuantEntitlements
    from al1s.adapters.postgres.quant_health import database_available
    from al1s.adapters.postgres.quant_instruments import QuantInstruments
    from al1s.adapters.postgres.quant_market_data import QuantMarketData
    from al1s.adapters.postgres.quant_orders import QuantOrders
    from al1s.adapters.postgres.quant_settings import QuantSettingsRepository
    from al1s.quant.backtest_service import QuantBacktest
    from al1s.quant.corporate_service import CorporateService
    from al1s.quant.execution import PaperExecution
    from al1s.quant.health import QuantHealth
    from al1s.quant.paper_engine import apply_paper_entitlement
    from al1s.quant.process import create_process_app
    from al1s.quant.retention import QuantRetention
    from al1s.quant.service import QuantService
    from al1s.quant.source_client import MarketSource
    from al1s.quant.trade import QuantTrade
    from al1s.secrets.security import FernetSecretCipher

    token = os.environ.get("AL1S_QUANT_CONTROL_TOKEN", "")
    dsn = os.environ.get("AL1S_QUANT_DATABASE_URL", "")
    if not dsn or version("akquant") != "0.3.71" or not hasattr(Engine, "apply_paper_entitlement"):
        raise ValueError("quant_runtime_configuration_invalid")
    database = create_engine(
        dsn, pool_size=5, max_overflow=2, pool_pre_ping=True, connect_args={"connect_timeout": 5}
    )
    sessions = sessionmaker(database, expire_on_commit=False)
    credential_key = os.environ.get("AL1S_QUANT_CREDENTIAL_KEY", "")
    credentials = (
        QuantCredentials(sessions, FernetSecretCipher(credential_key))
        if len(credential_key.encode()) >= 32
        else None
    )
    source, accounts, instruments = (
        MarketSource(
            credential=credentials.get if credentials else lambda _: None,
            configured=credentials.configured if credentials else set,
            credential_snapshot=credentials.snapshot if credentials else None,
        ),
        QuantAccounts(sessions),
        QuantInstruments(sessions),
    )
    orders, cache, backtests = (
        QuantOrders(sessions),
        QuantMarketData(sessions),
        QuantBacktests(sessions),
    )
    corporate = CorporateService(
        source, QuantCorporateSync(sessions), QuantEntitlements(sessions, apply_paper_entitlement)
    )
    trade = QuantTrade(source, accounts, instruments, orders, cache, corporate=corporate)
    backtest = QuantBacktest(source, accounts, instruments, cache, backtests)
    service = QuantService(accounts, orders, trade=trade.submit, backtest=backtest.submit)
    health = QuantHealth(
        database=lambda: database_available(database),
        source=source.health,
        engine=lambda: {
            "version": version("akquant"),
            "entitlement_supported": hasattr(Engine, "apply_paper_entitlement"),
        },
    )
    app = create_process_app(
        token,
        command_handler=service.execute,
        executor=PaperExecution(orders, source, cache),
        backtests=backtests,
        corporate=corporate,
        health=health,
        retention=QuantRetention(QuantSettingsRepository(sessions), cache, source),
    )
    config = uvicorn.Config(
        app, log_level="warning", access_log=False, timeout_graceful_shutdown=20
    )
    try:
        with secure_socket(Path("/run/al1s-quant")) as sock:
            uvicorn.Server(config).run(sockets=[sock])
    finally:
        database.dispose()


if __name__ == "__main__":
    run()
