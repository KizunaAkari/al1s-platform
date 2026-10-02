"""Issue a terminal TLS leaf with an existing CA; never replace the CA."""
import argparse
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ca-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--hostname', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', args.hostname):
        raise SystemExit('Invalid DNS hostname')
    destination = args.output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if any((destination / name).exists() for name in ('editor.crt', 'editor.key')):
        raise SystemExit('Refusing to replace an existing certificate')
    ca = x509.load_pem_x509_certificate((args.ca_dir / 'ca.crt').read_bytes())
    ca_key = serialization.load_pem_private_key((args.ca_dir / 'ca.key').read_bytes(), password=None)
    if ca.public_key().public_numbers() != ca_key.public_key().public_numbers():
        raise SystemExit('CA key mismatch')
    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    cert = (x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, args.hostname)]))
        .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(min(now + timedelta(days=90), ca.not_valid_after_utc))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(args.hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
        .sign(ca_key, hashes.SHA256()))
    (destination / 'editor.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (destination / 'editor.key').chmod(0o600)
    (destination / 'editor.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print('Terminal leaf issued; existing CA retained; private key not displayed.')


if __name__ == '__main__':
    main()
