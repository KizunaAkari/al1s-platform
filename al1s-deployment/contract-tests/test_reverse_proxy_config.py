from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
FRONTEND_CONFIG = WORKSPACE / "al1s-frontend" / "nginx.conf"
TLS_CONFIG = WORKSPACE / "al1s-deployment" / "compose" / "tls" / "nginx.conf"


def test_frontend_resolves_recreated_backend_through_docker_dns() -> None:
    config = FRONTEND_CONFIG.read_text(encoding="utf-8")

    assert "resolver 127.0.0.11 valid=10s ipv6=off;" in config
    assert "set $backend_upstream backend:8000;" in config
    assert "proxy_pass http://$backend_upstream;" in config
    assert "proxy_pass http://backend:8000" not in config


def test_tls_gateway_resolves_recreated_backend_and_object_gateway() -> None:
    config = TLS_CONFIG.read_text(encoding="utf-8")

    assert "resolver 127.0.0.11 valid=10s ipv6=off;" in config
    assert "set $frontend_upstream backend:8000;" in config
    assert "set $s3_upstream seaweed-s3:8333;" in config
    assert "proxy_pass http://$frontend_upstream;" in config
    assert "proxy_pass http://$s3_upstream;" in config
    assert "proxy_pass http://backend:8000" not in config
    assert "proxy_pass http://seaweed-s3:8333" not in config


def test_tls_containers_mount_only_required_leaf_material():
    config = (WORKSPACE / "al1s-deployment/compose/compose.tls.yaml").read_text(encoding="utf-8")
    assert "./tls/generated:" not in config
    assert "ca.key" not in config
    assert "./tls/generated/ca.crt:/mosquitto/certs/ca.crt:ro" in config
    assert "./tls/generated/server.key:/etc/al1s/tls/server.key:ro" in config
