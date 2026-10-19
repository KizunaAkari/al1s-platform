from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_qq_producer_and_candidate_use_same_identity_bootstrap():
    for name in ("Dockerfile.qq", "Dockerfile.qq-candidate"):
        source = (ROOT / "al1s-deployment/bots" / name).read_text()
        assert 'ENTRYPOINT ["python","-m","al1s.bots.qq_identity.startup"]' in source
        assert "qq_device_identity_v1" in source
    supervisor = (ROOT / "al1s-deployment/bots/qq-supervisord.conf").read_text()
    assert "command=python -m al1s.bots.qq_identity.native_start" in supervisor
    assert "exitcodes=0,78" in supervisor
