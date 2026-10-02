"""Historical overlay builds must require an explicitly reviewed base image."""

from pathlib import Path


def test_overlay_dockerfiles_have_no_implicit_historical_base() -> None:
    root = Path(__file__).resolve().parents[1] / "platform"
    overlays = sorted(root.glob("Dockerfile.*-update"))
    assert overlays
    for dockerfile in overlays:
        text = dockerfile.read_text(encoding="utf-8")
        assert "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}" in text, dockerfile.name
        assert "ARG BASE_IMAGE=" not in text, dockerfile.name
