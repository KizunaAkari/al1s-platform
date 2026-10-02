import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

spec = importlib.util.spec_from_file_location('repair_ca', Path(__file__).resolve().parents[1] / 'tools/repair_ca.py')
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def legacy_ca():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Isolated test CA')])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return cert, key


def test_repair_preserves_identity_and_validity_but_not_serial():
    cert, key = legacy_ca()
    repaired = module.repair_certificate(cert, key)
    assert repaired.subject == cert.subject
    assert repaired.public_key() == cert.public_key()
    assert repaired.serial_number != cert.serial_number
    assert repaired.not_valid_after_utc == cert.not_valid_after_utc
    assert repaired.not_valid_before_utc == cert.not_valid_before_utc
    usage = repaired.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical and usage.value.key_cert_sign and usage.value.crl_sign
    repaired.verify_directly_issued_by(repaired)
    for extension in cert.extensions:
        assert repaired.extensions.get_extension_for_oid(extension.oid) == extension


def test_repair_rejects_wrong_key():
    cert, _ = legacy_ca()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(ValueError, match='matching'):
        module.repair_certificate(cert, other)


def test_repair_does_not_override_existing_usage():
    cert, key = legacy_ca()
    repaired = module.repair_certificate(cert, key)
    with pytest.raises(ValueError, match='already'):
        module.repair_certificate(repaired, key)
