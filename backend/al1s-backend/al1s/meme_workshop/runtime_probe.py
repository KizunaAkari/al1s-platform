"""Image capability check without credentials, database access, or chat traffic."""

import json
import importlib
import os
import shutil
import subprocess
from importlib.metadata import version as package_version
from io import BytesIO
from pathlib import Path

from opencc import OpenCC
from PIL import Image

from al1s.meme_workshop.contracts import Layout, RenderInputs
from al1s.meme_workshop.renderer import render
from al1s.meme_workshop.runtime_identity import read_runtime_identity


def prove_local_ai(supervisor: Path, *, legacy_paths=(Path('/opt/al1s-astrbot-venv'),
                        Path('/opt/al1s-astrbot-src'), Path('/opt/al1s-astrbot-plugins'))):
    config = supervisor.read_text(encoding='utf-8')
    if 'program:astrbot' in config or any(path.exists() for path in legacy_paths):
        raise ValueError('qq_ai_astrbot_runtime_present')
    if 'program:meme-backend' not in config:
        raise ValueError('qq_ai_supervisor_missing')
    for name, expected in (('openai', '3.31.0'), ('google-genai', '2.33.0'), ('websockets', '17.1')):
        if package_version(name) != expected:
            raise ValueError('qq_ai_dependency_proof_invalid')
    for name in ('openai', 'google.genai', 'httpx2', 'rank_bm25', 'rapidfuzz',
                 'al1s.bot_ai.executor', 'al1s.bot_ai.sticker_library'):
        importlib.import_module(name)
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise ValueError('qq_ai_dependency_proof_invalid')


def prove_runtime(manifest: Path, package: Path, role: str, *,
                  supervisor: Path = Path("/etc/al1s-qq-supervisord.conf")) -> dict[str, object]:
    identity = read_runtime_identity(manifest)
    if identity.role != role:
        raise ValueError("bot_runtime_role_invalid")
    native = json.loads(package.read_text(encoding="utf-8"))["version"]
    if native != identity.native_version:
        raise ValueError("bot_native_version_mismatch")
    if identity.qq_protocol_mode == "pmhq":
        from al1s.bots.qq_headed.probe import prove_headed
        if os.environ.get("PROTOCOL_MODE", "pmhq") != "pmhq":
            raise ValueError("qq_headed_mode_mismatch")
        prove_headed(identity, supervisor)
    if identity.qq_device_identity_v1:
        from al1s.bots.qq_identity.apply import default_device_name
        if (role != "qq" or os.environ.get("AL1S_QQ_BOOTSTRAP_VERSION") != "1"
                or "command=python -m al1s.bots.qq_identity.native_start"
                not in supervisor.read_text()):
            raise ValueError("qq_identity_bootstrap_missing")
        default_device_name(bytes.fromhex("12" * 16), native)
    if identity.qq_device_name_override != (identity.qq_device_identity_v1 and native == "8.3.2"):
        raise ValueError("qq_identity_native_capability_invalid")
    if identity.qq_quant_v1:
        if "program:quant-backend" not in supervisor.read_text(encoding="utf-8"):
            raise ValueError("qq_quant_supervisor_missing")
        code = ("import importlib.metadata as m; from akquant import Engine; "
                "assert m.version('akquant') == '0.3.71'; assert m.version('akshare') == '1.19.3'; "
                "assert hasattr(Engine, 'apply_paper_entitlement'); "
                "import sqlalchemy, psycopg, fastapi; "
                "import al1s.quant.process, al1s.quant.socket_runtime, al1s.quant.trade, "
                "al1s.quant.backtest_service, al1s.quant.corporate_service")
        result = subprocess.run(["/opt/al1s-quant-venv/bin/python", "-c", code],
                                capture_output=True, timeout=30, check=False,
                                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt/al1s",
                                     "PYTHONDONTWRITEBYTECODE": "1"})
        if result.returncode:
            raise ValueError("qq_quant_dependency_proof_invalid")
    if OpenCC("t2s").convert("測試") != "测试":
        raise ValueError("bot_text_runtime_invalid")
    if identity.qq_freqtrade_v1:
        if 'program:freqtrade-backend' not in supervisor.read_text(encoding='utf-8'):
            raise ValueError('qq_freqtrade_supervisor_missing')
        checked = subprocess.run(['/opt/al1s-freqtrade-venv/bin/python', '-c',
            "import freqtrade; import psycopg; import al1s.freqtrade.runtime; "
            "assert freqtrade.__version__ == '2026.9'"], capture_output=True, timeout=30,
            env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': '/opt/al1s', 'PYTHONDONTWRITEBYTECODE': '1'})
        if checked.returncode:
            raise ValueError('qq_freqtrade_dependency_proof_invalid')
    if identity.qq_freqtrade_futures_v1:
        checked = subprocess.run(['/opt/al1s-freqtrade-venv/bin/python', '-c',
            "from decimal import Decimal; from al1s.freqtrade.FuturesManualPaperStrategy import FuturesManualPaperStrategy; "
            "from al1s.freqtrade.leverage import leverage_limit; "
            "assert FuturesManualPaperStrategy.can_short; "
            "assert leverage_limit('BTC/USDT:USDT',Decimal('100')) > 1"],
            capture_output=True, timeout=30, env={'PATH':'/usr/bin:/bin','PYTHONPATH':'/opt/al1s',
                                                'PYTHONDONTWRITEBYTECODE':'1'})
        if checked.returncode:
            raise ValueError('qq_freqtrade_futures_dependency_proof_invalid')
    if identity.qq_freqtrade_visual_v1:
        checked = subprocess.run(['/opt/al1s-freqtrade-venv/bin/python', '-c',
            "import matplotlib; import al1s.freqtrade.charts; import al1s.freqtrade.exit_notices; "
            "from al1s.freqtrade.contracts import DEFAULT_COMMANDS; "
            "assert DEFAULT_COMMANDS['chart'] == '币走势' and DEFAULT_COMMANDS['f_chart'] == '合约走势'"],
            capture_output=True, timeout=30,
            env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': '/opt/al1s', 'PYTHONDONTWRITEBYTECODE': '1'})
        if checked.returncode:
            raise ValueError('qq_freqtrade_visual_dependency_proof_invalid')
    if identity.qq_freqtrade_capital_v1:
        checked = subprocess.run(['/opt/al1s-freqtrade-venv/bin/python', '-c',
            "import al1s.freqtrade.native_capital as native_capital; "
            "import al1s.freqtrade.native_runner; "
            "assert native_capital.EXTENSION_VERSION == 1"],
            capture_output=True, timeout=30,
            env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': '/opt/al1s', 'PYTHONDONTWRITEBYTECODE': '1'})
        if checked.returncode:
            raise ValueError('qq_freqtrade_capital_dependency_proof_invalid')
    if identity.qq_command_replies_v1:
        from al1s.trading_replies.catalog import catalogue
        from al1s.trading_replies.render import preview
        if len(catalogue("quant")) != 8 or len(catalogue("freqtrade")) != 19:
            raise ValueError("qq_command_replies_catalogue_invalid")
        if preview("quant", "buy", "模拟买入", {"main": "{code}"}, "main") != "300750":
            raise ValueError("qq_command_replies_render_invalid")
    if identity.qq_ai_v1:
        prove_local_ai(supervisor)
    if identity.qq_ai_personas_v1:
        from al1s.bot_ai.messages import persona_command
        from al1s.adapters.postgres.bot_ai_repository import BotAiRepository
        if persona_command("切换人格 大肥鱼") != "大肥鱼" or not callable(BotAiRepository.select_chat_profile):
            raise ValueError("qq_ai_personas_dependency_proof_invalid")
    if identity.qq_ai_network_routes_v1:
        from unittest.mock import patch
        from al1s.bot_ai.provider_clients import create_client
        from al1s.bot_ai.settings import AiProviderView
        direct = AiProviderView(adapter_kind="openai_whisper_api", model_id="proof",
                                base_url="", voice="", secret_configured=True, network_mode="direct")
        with patch("httpx2.AsyncClient") as http, patch("openai.AsyncOpenAI"):
            create_client(direct, "fixture")
            http.assert_called_once_with(proxy=None, trust_env=False, timeout=20)
    if identity.qq_ai_group_reply_v1:
        from datetime import UTC, datetime
        from uuid import uuid4
        from al1s.bot_ai.messages import parse_ai_event
        from al1s.bot_ai.settings import AiSettingsRecord
        group_settings = AiSettingsRecord(service_id=uuid4(), enabled=True, groups=["55"],
            reply_all_groups=["55"], desired_version=1, applied_version=1)
        group_event = {"post_type": "message", "message_type": "group", "self_id": 123,
            "user_id": 99, "group_id": 55, "message_id": 1,
            "time": int(datetime.now(UTC).timestamp()),
            "message": [{"type": "text", "data": {"text": "proof"}}]}
        if parse_ai_event(group_event, group_settings) is None:
            raise ValueError("qq_ai_group_reply_dependency_proof_invalid")
    if identity.qq_ai_smart_reply_v1:
        from al1s.bot_ai.managed.smart_contract import AiSmartOptions
        from al1s.bot_ai.managed.smart_reply import decode_decision
        if decode_decision('{"should_reply":false,"channels":[]}', {"text"}, "context").should_reply:
            raise ValueError("qq_ai_smart_reply_dependency_proof_invalid")
        if not (Path(__file__).resolve().parents[1] / "bot_ai/sticker_core/LICENSE").is_file():
            raise ValueError("qq_ai_stealer_dependency_proof_invalid")
    if identity.qq_comfy_drawing_v1:
        for name in ('onnxruntime', 'multipart', 'al1s.bot_drawing.runner',
                     'al1s.bot_drawing.service', 'al1s.api.bot_drawing'):
            importlib.import_module(name)
    source = BytesIO()
    Image.new("RGBA", (64, 64), "#123456").save(source, format="PNG")
    result = render(
        Layout(engine="petpet", width=64, height=64, frame_duration_ms=50),
        RenderInputs(avatars=[source.getvalue()]),
        {},
    )
    image = Image.open(BytesIO(result.body))
    if (
        image.format != "GIF"
        or getattr(image, "n_frames", 0) < 2
        or image.info.get("duration") != 50
    ):
        raise ValueError("bot_render_proof_invalid")
    return {
        "role": role,
        "native_version": native,
        "runtime_version": identity.runtime_version,
        "render_proof": True,
        "qq_device_identity_v1": identity.qq_device_identity_v1,
        "qq_device_name_override": identity.qq_device_name_override,
        "qq_protocol_mode": identity.qq_protocol_mode,
        "qq_client_version": identity.qq_client_version,
        "pmhq_version": identity.pmhq_version,
        "qq_headed_v1": identity.qq_headed_v1,
        "qq_quant_v1": identity.qq_quant_v1,
        'qq_freqtrade_v1': identity.qq_freqtrade_v1,
        'qq_freqtrade_futures_v1': identity.qq_freqtrade_futures_v1,
        'qq_freqtrade_visual_v1': identity.qq_freqtrade_visual_v1,
        'qq_freqtrade_capital_v1': identity.qq_freqtrade_capital_v1,
        'qq_command_replies_v1': identity.qq_command_replies_v1,
        'qq_ai_v1': identity.qq_ai_v1,
        'qq_ai_personas_v1': identity.qq_ai_personas_v1,
        'qq_ai_network_routes_v1': identity.qq_ai_network_routes_v1,
        'qq_ai_group_reply_v1': identity.qq_ai_group_reply_v1,
        'qq_ai_smart_reply_v1': identity.qq_ai_smart_reply_v1,
        'qq_comfy_drawing_v1': identity.qq_comfy_drawing_v1,
    }


if __name__ == "__main__":
    role = os.environ.get("AL1S_MEME_ROLE", "")
    package = Path("/app/llbot/package.json" if role == "qq" else "/app/package.json")
    print(json.dumps(prove_runtime(Path("/opt/al1s/runtime-manifest.json"), package, role)))
