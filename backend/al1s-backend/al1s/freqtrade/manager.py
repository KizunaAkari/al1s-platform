import base64
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from threading import Condition, RLock
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from al1s.freqtrade.contracts import FreqtradeSettings
from al1s.freqtrade.exit_notices import ExitNotices
from al1s.freqtrade.native_config import native_config


def confirm_exit(result: object, trade_id: int) -> None:
    if isinstance(result, dict):
        message, status = result.get("result"), result.get("status")
        if any(isinstance(value, str) and value.lower().startswith("error")
               for value in (message, status)):
            raise ValueError("框架未受理该卖出或平仓委托，请核对持仓后重试。")
        if message == f"Created exit order for trade {trade_id}.":
            return
    raise OSError("freqtrade_exit_result_unconfirmed")


@dataclass
class NativeInstance:
    process: subprocess.Popen
    port: int
    password: str
    last_used: float
    fee: str
    capital_version: int = field(default=0, init=False)
    profile: object = None
    notice_lock: object = field(default_factory=RLock)

    def call(self, path: str, data: dict | None = None):
        body = json.dumps(data).encode() if data is not None else None
        auth = base64.b64encode(("al1s:" + self.password).encode()).decode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/v1/{path}",
            data=body,
            headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=20) as response:
                content = response.read(1048577)
                if len(content) > 1048576:
                    raise OSError("freqtrade_response_oversize")
                try:
                    return json.loads(content)
                except (ValueError, UnicodeError) as error:
                    raise OSError("freqtrade_response_unconfirmed") from error
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read(4096))
            except (ValueError, UnicodeDecodeError):
                payload = {}
            business_error = payload.get("error", "") if isinstance(payload, dict) else ""
            if (
                error.code == 502
                and isinstance(business_error, str)
                and business_error.startswith("Error querying /api/v1/")
            ):
                raise ValueError(business_error[:300]) from None
            if 400 <= error.code < 500:
                detail = (
                    payload.get("detail", "订单被框架拒绝")
                    if isinstance(payload, dict)
                    else "订单被框架拒绝"
                )
                raise ValueError(str(detail)[:300]) from None
            raise OSError("freqtrade_native_error") from None


class FreqtradeManager:
    def __init__(self, repository, native_database: str, directory: Path):
        self.repository = repository
        self.database_url = make_url(native_database)
        if self.database_url.get_backend_name() != "postgresql":
            raise ValueError("freqtrade_postgres_required")
        self.database = create_engine(native_database, pool_size=1, max_overflow=1)
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.instances: dict[UUID, NativeInstance] = {}
        self.lock = RLock()
        self.condition = Condition(self.lock)
        self._busy: set[UUID] = set()
        self._closing = False
        self.capacity = 5
        self.market_success = None
        self.notices = ExitNotices(repository)

    def has_namespace(self, profile) -> bool:
        with self.database.connect() as connection:
            return bool(connection.scalar(text("select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                                               "where n.nspname=:name and c.relname='trades' and c.relkind='r'"),
                                          {"name": "ft_" + profile.id.hex}))

    def ensure(self, profile, settings: FreqtradeSettings) -> NativeInstance:
        market = getattr(profile, "market", "spot")
        fee = settings.futures_fee if market == "futures" else settings.fee
        identity = profile.id
        with self.condition:
            while identity in self._busy and not self._closing:
                self.condition.wait()
            if self._closing:
                raise ValueError("freqtrade_manager_stopping")
            current = self.instances.get(profile.id)
            if current is not None and current.process.poll() is None:
                if current.fee == str(fee):
                    current.last_used = time.monotonic()
                    return current
            else:
                current = None
            self.instances = {
                key: value for key, value in self.instances.items()
                if key in self._busy or value.process.poll() is None
            }
            occupied = set(self.instances) | self._busy
            if identity not in occupied and len(occupied) >= settings.max_active_accounts:
                raise ValueError("模拟账户活动容量已满，请稍后重试；既有资金和持仓保留。")
            self.capacity = settings.max_active_accounts
            self._busy.add(identity)
        try:
            if current is not None:
                if any(trade.get("has_open_orders") for trade in current.call("status")):
                    raise ValueError("账户存在在途委托，待完成后应用新费率。")
                self._stop(identity, mark_inactive=False)
            instance = self._start(profile, settings)
            try:
                self.repository.set_profile(identity, market=market, native_active=True)
            except Exception:
                self._terminate(instance)
                raise
            with self.lock:
                self.instances[identity] = instance
            return instance
        finally:
            with self.condition:
                self._busy.discard(identity)
                self.condition.notify_all()

    def _start(self, profile, settings: FreqtradeSettings) -> NativeInstance:
        namespace = "ft_" + profile.id.hex
        # Only a system-generated UUID can enter this trusted SQL identifier.
        with self.database.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{namespace}"')
        url = self.database_url.update_query_dict({"options": "-csearch_path=" + namespace})
        directory = self.directory / profile.id.hex
        directory.mkdir(exist_ok=True, mode=0o700)
        market = getattr(profile, "market", "spot")
        strategy = (
            "FuturesManualPaperStrategy.py" if market == "futures" else "ManualPaperStrategy.py"
        )
        shutil.copyfile(Path(__file__).with_name(strategy), directory / strategy)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        password = secrets.token_urlsafe(40)
        config = native_config(
            settings,
            initial_cash=profile.initial_cash,
            db_url=url.render_as_string(hide_password=False),
            directory=str(directory),
            port=port,
            password=password,
            market=market,
        )
        path = directory / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        path.chmod(0o600)
        env = {key: os.environ[key] for key in ("PATH", "SSL_CERT_FILE", "TZ") if key in os.environ}
        env.update(
            HOME=str(directory),
            PYTHONPATH=str(Path(__file__).resolve().parents[2]),
            PYTHONUNBUFFERED="1",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            NUMEXPR_NUM_THREADS="1",
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "al1s.freqtrade.native_runner",
                "trade",
                "--config",
                str(path),
                "--logfile",
                str(directory / "freqtrade.log"),
            ],
            env=env,
            cwd=directory,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        fee = settings.futures_fee if market == "futures" else settings.fee
        instance = NativeInstance(process, port, password, time.monotonic(), str(fee), profile)
        try:
            self._wait(instance)
        except Exception:
            self._terminate(instance)
            raise
        return instance

    @staticmethod
    def _wait(instance: NativeInstance) -> None:
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            if instance.process.poll() is not None:
                raise ValueError("Freqtrade启动失败，账户数据已保留，请管理员检查。")
            try:
                info = instance.call("show_config")
                if info.get("state") == "running" and info.get("dry_run") is True:
                    if instance.call("al1s/capital").get("extension") == 1:
                        instance.capital_version = 1
                        return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise ValueError("Freqtrade行情初始化超时，尚未执行买卖。")

    @staticmethod
    def _terminate(instance: NativeInstance) -> None:
        if instance.process.poll() is None:
            os.killpg(instance.process.pid, signal.SIGTERM)
            try:
                instance.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(instance.process.pid, signal.SIGKILL)
                instance.process.wait(timeout=5)

    def _stop(self, identity: UUID, *, mark_inactive: bool) -> None:
        with self.lock:
            instance = self.instances.get(identity)
        if (instance is not None and instance.profile is not None
            and instance.profile.market == "futures" and instance.process.poll() is None):
            with instance.notice_lock:
                while not self.notices.catch_up(instance.profile, instance):
                    pass
        if instance is not None:
            self._terminate(instance)
            with self.lock:
                if self.instances.get(identity) is instance:
                    self.instances.pop(identity)
        if mark_inactive:
            self.repository.mark_inactive([identity])

    def idle_cleanup(self) -> None:
        with self.lock:
            candidates = list(self.instances.items())
        settings_by_id = self.repository.runtime_settings([identity for identity, _ in candidates])
        stopped = []
        try:
            for identity, instance in candidates:
                if time.monotonic() - instance.last_used < 60:
                    continue
                settings = settings_by_id.get(identity)
                if settings is None or time.monotonic() - instance.last_used < settings.idle_seconds:
                    continue
                reserved = False
                try:
                    if instance.call("status"):
                        continue
                    with self.condition:
                        if (self._closing or identity in self._busy
                                or self.instances.get(identity) is not instance
                                or time.monotonic() - instance.last_used < settings.idle_seconds):
                            continue
                        self._busy.add(identity)
                        reserved = True
                    self._stop(identity, mark_inactive=False)
                    stopped.append(identity)
                    reserved = False
                except (OSError, ValueError):
                    continue
                finally:
                    if reserved:
                        with self.condition:
                            self._busy.discard(identity)
                            self.condition.notify_all()
            self.repository.mark_inactive(stopped)
        finally:
            with self.condition:
                self._busy.difference_update(stopped)
                self.condition.notify_all()

    def close(self) -> None:
        with self.condition:
            self._closing = True
            self.condition.notify_all()
            while self._busy:
                self.condition.wait()
            identities = list(self.instances)
        for identity in identities:
            try:
                self._stop(identity, mark_inactive=False)
            except Exception:
                # Keep native_active for catch-up after restart, but never orphan a writer.
                with self.lock:
                    instance = self.instances.pop(identity, None)
                if instance is not None:
                    self._terminate(instance)
        self.database.dispose()
