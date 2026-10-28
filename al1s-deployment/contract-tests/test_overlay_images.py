"""Historical overlay builds must require an explicitly reviewed base image."""

from pathlib import Path
import re


def test_overlay_dockerfiles_have_no_implicit_historical_base() -> None:
    root = Path(__file__).resolve().parents[1] / "platform"
    overlays = sorted(root.glob("Dockerfile.*-update"))
    assert overlays
    for dockerfile in overlays:
        text = dockerfile.read_text(encoding="utf-8")
        base = re.search(r"^ARG ([A-Z_]+)\nFROM \$\{\1\}(?:\n|$)", text, re.MULTILINE)
        assert base is not None, dockerfile.name
        assert f"ARG {base[1]}=" not in text, dockerfile.name
