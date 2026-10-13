"""Verify offline certificate import without existing-file replacement."""
import io
import stat
import sys
import unittest
import zipfile
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.import_certificate import certificate_archive, decode_archive, validate_pem, write_private_pem
from cert_fixture import KEY, CERTIFICATE, FIXTURES


class CertificateImportTests(unittest.TestCase):
    def test_own_apk_certificate_is_imported_with_private_permissions(self):
        password = 'test-archive-only'
        archive = pkcs12.serialize_key_and_certificates(b'test-client', KEY, CERTIFICATE, None,
                    serialization.BestAvailableEncryption(password.encode()))
        carrier = io.BytesIO()
        with zipfile.ZipFile(carrier, 'w') as apk:
            apk.writestr('res/raw/client_mobile_android.p12', archive)
        carrier.seek(0)
        pem = decode_archive(certificate_archive(carrier), password)
        validate_pem(pem)
        destination = write_private_pem(pem, FIXTURES / 'imported-client.pem')
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        original = destination.read_bytes()
        with self.assertRaises(FileExistsError):
            write_private_pem(b'changed', destination)
        self.assertEqual(destination.read_bytes(), original)

    def test_wrong_archive_password_creates_no_certificate(self):
        archive = pkcs12.serialize_key_and_certificates(b'test-client', KEY, CERTIFICATE, None,
                    serialization.BestAvailableEncryption(b'test-archive-only'))
        with self.assertRaises(ValueError):
            decode_archive(archive, 'incorrect-test-password')

    def test_missing_or_ambiguous_apk_certificate_is_rejected(self):
        for names in [[], ['res/raw/client_mobile_android.p12', 'assets/client_mobile_android.p12']]:
            carrier = io.BytesIO()
            with zipfile.ZipFile(carrier, 'w') as apk:
                for name in names:
                    apk.writestr(name, b'fixture')
            carrier.seek(0)
            with self.assertRaises(ValueError):
                certificate_archive(carrier)


if __name__ == '__main__':
    unittest.main()
