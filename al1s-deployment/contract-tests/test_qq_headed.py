from pathlib import Path


def test_headed_image_and_supervisor_preserve_container_boundary():
    root = Path(__file__).resolve().parents[1] / "bots"
    recipe = (root / "Dockerfile.qq-headed").read_text()
    supervisor = (root / "qq-headed-supervisord.conf").read_text()
    assert "apk" not in recipe
    assert "PROTOCOL_MODE=pmhq" in recipe
    assert "qq_headed.startup" in recipe
    assert "config['webui']['host']=''" in recipe
    for name in ("xvfb", "pmhq", "llbot", "meme-backend"):
        assert f"[program:{name}]" in supervisor
    assert "-nolisten tcp" in supervisor
    assert "PROTOCOL_MODE: ${AL1S_QQ_PROTOCOL_MODE:-direct}" in (root / "compose.yaml").read_text()
