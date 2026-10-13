"""Generate independent TLS fixtures for each test run, without committed keys."""
import datetime
import os
import tempfile
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

parent = os.environ.get('VIDAA_TEST_ARTIFACTS_DIR')
if parent:
    Path(parent).mkdir(parents=True, exist_ok=True)
FIXTURES = Path(tempfile.mkdtemp(prefix='vidaa-remote-tests-', dir=parent))
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
now = datetime.datetime.now(datetime.timezone.utc)
CERTIFICATE = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
               .public_key(KEY.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(minutes=1))
               .not_valid_after(now + datetime.timedelta(days=1))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
               .sign(KEY, hashes.SHA256()))
certificate = CERTIFICATE.public_bytes(serialization.Encoding.PEM)
key = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption())
for name, value in [('cert.pem', certificate), ('key.pem', key), ('client.pem', certificate + key)]:
    fd = os.open(FIXTURES / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(value)
# Files are retained for debugging; no application or test removes user files.
