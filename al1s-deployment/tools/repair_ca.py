"""Repair a legacy private CA's missing signing usage without rotating its key.

Writes a new certificate to a new path only. Backups, installation, service reloads,
and trust-store changes are deliberately separate operator actions.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def repair_certificate(cert: x509.Certificate, key: rsa.RSAPrivateKey) -> x509.Certificate:
    if cert.subject != cert.issuer or cert.public_key() != key.public_key():
        raise ValueError("Expected a self-issued CA and its matching RSA private key")
    cert.verify_directly_issued_by(cert)
    if not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("Certificate is not a CA")
    if not cert.not_valid_before_utc <= datetime.now(UTC) < cert.not_valid_after_utc:
        raise ValueError("CA is outside its validity interval")
    try:
        cert.extensions.get_extension_for_class(x509.KeyUsage)
    except x509.ExtensionNotFound:
        pass
    else:
        raise ValueError("CA already has Key Usage; refusing to change its policy")
    builder = (x509.CertificateBuilder().subject_name(cert.subject).issuer_name(cert.issuer)
               .public_key(cert.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(cert.not_valid_before_utc).not_valid_after(cert.not_valid_after_utc))
    for extension in cert.extensions:
        builder = builder.add_extension(extension.value, critical=extension.critical)
    usage = x509.KeyUsage(digital_signature=False, content_commitment=False,
                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                         key_cert_sign=True, crl_sign=True, encipher_only=None, decipher_only=None)
    return builder.add_extension(usage, critical=True).sign(key, hashes.SHA256())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--certificate', type=Path, required=True)
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cert = x509.load_pem_x509_certificate(args.certificate.read_bytes())
    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Only the existing RSA development CA is supported")
    repaired = repair_certificate(cert, key)
    with args.output.open('xb') as stream:
        stream.write(repaired.public_bytes(serialization.Encoding.PEM))
    print('Public CA certificate repaired; private key and live configuration unchanged')


if __name__ == '__main__':
    main()
