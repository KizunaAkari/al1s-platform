"""Meme rendering belongs to the existing Bot deployment, with platform UI/proxy."""
import configparser
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_bot_python_runtime_carries_public_ca_bundle_and_checks_trust_store():
    dockerfile = (ROOT / "backend/al1s-bot/Dockerfile").read_text()
    assert "COPY --from=python-runtime /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt" in dockerfile
    assert "ssl.create_default_context().cert_store_stats()['x509_ca'] > 0" in dockerfile
    assert "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in dockerfile


def test_meme_feature_preserves_existing_bot_containers_and_internal_control_port():
    config = yaml.safe_load((ROOT / "al1s-deployment/bots/compose.yaml").read_text())
    assert config["name"] == "al1s-bot"
    assert set(config["services"]) == {"llbot", "discord-worker"}
    worker = config["services"]["discord-worker"]
    assert worker["container_name"] == "al1s-bot-discord"
    assert config["services"]["llbot"]["container_name"] == "al1s-bot-qq"
    assert worker["build"] == {"context": "../../backend", "dockerfile": "al1s-bot/Dockerfile"}
    assert not worker.get("ports")
    assert "AL1S_MEME_DATABASE_URL" in worker["environment"]
    assert worker["volumes"] == ["discord-worker-state:/var/lib/al1s-plachta"]


def test_meme_backend_is_supervised_in_bot_without_a_platform_render_process():
    bot = configparser.ConfigParser()
    bot.read(ROOT / "backend/al1s-bot/supervisord.conf")
    assert {name for name in bot.sections() if name.startswith("program:")} == {
        "program:discord", "program:meme-backend",
    }
    assert bot["program:meme-backend"]["command"] == "python -m al1s.meme_workshop.runtime_main"
    assert bot["program:meme-backend"]["stopasgroup"] == "true"
    assert bot["program:meme-backend"]["exitcodes"] == "0,78"
    platform = configparser.ConfigParser()
    platform.read(ROOT / "al1s-deployment/platform/supervisord.conf")
    assert "program:meme-backend" not in platform
    routes = (ROOT / "backend/al1s-backend/al1s/api/router.py").read_text()
    assert "meme_proxy" in routes
    assert "from al1s.api.meme_workshop" not in routes


def test_platform_and_bot_share_only_existing_control_network():
    bot = yaml.safe_load((ROOT / "al1s-deployment/bots/compose.yaml").read_text())
    assert set(bot["networks"]) == {"bot-network", "control-network"}
    assert bot["networks"]["control-network"]["external"] is True
    platform = yaml.safe_load((ROOT / "al1s-deployment/compose/compose.platform.yaml").read_text())
    environment = platform["services"]["backend"]["environment"]
    assert "AL1S_MEME_BACKEND_URL" in environment
    assert "AL1S_MEME_BACKEND_TOKEN" in environment


def test_qq_auto_login_uses_native_option_and_preserves_the_account_volume():
    config = yaml.safe_load((ROOT / "al1s-deployment/bots/compose.yaml").read_text())
    qq = config["services"]["llbot"]
    assert qq["environment"]["AUTO_LOGIN_QQ"] == "${AUTO_LOGIN_QQ:-}"
    assert qq["container_name"] == "al1s-bot-qq"
    assert qq["volumes"] == ["./llbot-data:/app/llbot/data:rw"]
    assert set(config["services"]) == {"llbot", "discord-worker"}
