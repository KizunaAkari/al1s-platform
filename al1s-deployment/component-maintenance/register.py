"""Operator registration only; never called by a browser or public API."""

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "backend" / "al1s-backend")
)
from al1s.components.host.container_config import relocate_settings


def issue_certificate(ca_root: Path, runtime: Path) -> tuple[Path, Path]:
    ca = x509.load_pem_x509_certificate((ca_root / "ca.crt").read_bytes())
    signer = serialization.load_pem_private_key(
        (ca_root / "ca.key").read_bytes(), password=None
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, "component-maintenance")]
            )
        )
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(min(now + timedelta(days=365), ca.not_valid_after_utc))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("component-maintenance")]), False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), False
        )
        .sign(signer, hashes.SHA256())
    )
    cert_path, key_path = (
        runtime / "container-server.crt",
        runtime / "container-server.key",
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)
    return cert_path, key_path


def environment_text(
    old: str, root: Path, output: Path, engine_root: str, image: str, platform_url: str
) -> str:
    connection_line = next(
        line for line in old.splitlines() if line.startswith("AL1S_COMPONENT_HOST=")
    )
    connection = json.loads(connection_line.split("=", 1)[1].strip().strip("'"))
    connection["url"] = "https://component-maintenance:8012"
    values = {
        "AL1S_COMPONENT_HOST": "'"
        + json.dumps(connection, separators=(",", ":"))
        + "'",
        "AL1S_COMPONENT_MAINTENANCE_IMAGE": image,
        "AL1S_COMPONENT_ROOT": engine_root,
        "AL1S_COMPONENT_DEPLOYMENT_SOURCE": root.as_posix(),
        "AL1S_COMPONENT_CONFIG": engine_root
        + "/"
        + output.relative_to(root).as_posix(),
        "AL1S_COMPONENT_PLATFORM_HOSTNAME": urlsplit(platform_url).hostname,
    }
    retained = [
        line for line in old.splitlines() if line.split("=", 1)[0] not in values
    ]
    return (
        "\n".join(retained + [f"{key}={value}" for key, value in values.items()]) + "\n"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--engine-root", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform-url", required=True)
    arguments = parser.parse_args()
    original = json.loads(arguments.config.read_text(encoding="utf-8"))
    root = Path(original["deployment_root"]).resolve()
    runtime = arguments.config.resolve().parent
    if not runtime.is_relative_to(root):
        raise ValueError("Registration must remain in the existing deployment root")
    for record in (Path(original["state_dir"]) / "operations").glob("*.json"):
        if json.loads(record.read_text(encoding="utf-8"))["state"] not in {
            "succeeded",
            "failed_safe",
        }:
            raise ValueError(
                "Reconcile unresolved component operations before registration"
            )
    environment = root / ".env.component-host"
    backup = runtime / "container-conversion"
    backup.mkdir(mode=0o700, exist_ok=True)
    if not (backup / "component-host.env.before").exists():
        shutil.copy2(environment, backup / "component-host.env.before")
    relocate_settings(original, arguments.engine_root, arguments.platform_url)
    ca_root = root / "compose" / "tls" / "generated"
    cert_path, key_path = issue_certificate(ca_root, runtime)
    original["certificate_file"], original["private_key_file"] = (
        str(cert_path),
        str(key_path),
    )
    settings = relocate_settings(
        original, arguments.engine_root, arguments.platform_url
    )
    output = runtime / "container.json"
    output.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.chmod(output, 0o600)
    text = environment_text(
        environment.read_text(encoding="utf-8"),
        root,
        output,
        arguments.engine_root,
        arguments.image,
        arguments.platform_url,
    )
    environment.write_text(text, encoding="utf-8")
    os.chmod(environment, 0o600)
    print(
        "Registered container config; existing credential and recovery directories preserved"
    )


if __name__ == "__main__":
    main()
