"""Generate ephemeral self-signed CA + server + client certs for the
TLS integration test. Uses the `cryptography` lib, no openssl-CLI
needed.

Hierarchy:
    ca.crt (self-signed CA, CN=test-ca)
        └─ server.crt (CN=localhost, SAN=DNS:localhost,IP:127.0.0.1)
        └─ client.crt (CN=test-client)

All certs valid for 1 day — test-only.
"""
import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _write_pem(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def _key_pem(key) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _cert_pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def make_ca_and_certs(out_dir: Path) -> dict:
    """Generate CA + server + client certs in out_dir.

    Returns dict of paths:
        {ca, server_cert, server_key, client_cert, client_key}
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    valid_until = now + datetime.timedelta(days=1)

    # --- CA ---------------------------------------------------------------
    ca_key = _new_key()
    ca_name = _name('test-ca')
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(valid_until)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    # --- Server cert (signed by CA, with localhost+127.0.0.1 SANs) -------
    server_key = _new_key()
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(_name('localhost'))
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(valid_until)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName('localhost'),
                x509.IPAddress(ipaddress.IPv4Address('127.0.0.1')),
            ]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # --- Client cert (signed by CA) --------------------------------------
    client_key = _new_key()
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(_name('test-client'))
        .issuer_name(ca_cert.subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(valid_until)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # --- Write all -------------------------------------------------------
    paths = {
        'ca': out_dir / 'ca.crt',
        'server_cert': out_dir / 'server.crt',
        'server_key': out_dir / 'server.key',
        'client_cert': out_dir / 'client.crt',
        'client_key': out_dir / 'client.key',
    }
    _write_pem(paths['ca'], _cert_pem(ca_cert))
    _write_pem(paths['server_cert'], _cert_pem(server_cert))
    _write_pem(paths['server_key'], _key_pem(server_key))
    _write_pem(paths['client_cert'], _cert_pem(client_cert))
    _write_pem(paths['client_key'], _key_pem(client_key))

    return paths
