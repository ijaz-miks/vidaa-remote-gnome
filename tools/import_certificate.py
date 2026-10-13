#!/usr/bin/env python3
"""Import a user-supplied VIDAA client certificate, without retaining an APK."""
import argparse
import getpass
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

PACKAGE = 'com.universal.remote.multi'
MAX_APK_BYTES = 256 * 1024 * 1024
MAX_P12_BYTES = 1024 * 1024


def certificate_archive(apk):
    with zipfile.ZipFile(apk) as archive:
        names = [name for name in archive.namelist()
                 if name.endswith('/client_mobile_android.p12')]
        if len(names) != 1:
            raise ValueError('The APK must contain exactly one client_mobile_android.p12 archive.')
        with archive.open(names[0]) as stream:
            data = stream.read(MAX_P12_BYTES + 1)
        if len(data) > MAX_P12_BYTES:
            raise ValueError('The certificate archive is unexpectedly large.')
        return data


def decode_archive(data, password):
    openssl = shutil.which('openssl')
    if not openssl:
        raise RuntimeError('Install OpenSSL before importing the certificate.')
    version = subprocess.run([openssl, 'version'], capture_output=True, text=True, timeout=10)
    major = re.match(r'OpenSSL (\d+)\.', version.stdout)
    legacy = ['-legacy'] if major and int(major.group(1)) >= 3 else []
    env = dict(os.environ, VIDAA_IMPORT_P12_PASSWORD=password)
    result = subprocess.run([openssl, 'pkcs12'] + legacy + ['-clcerts', '-nodes',
                             '-passin', 'env:VIDAA_IMPORT_P12_PASSWORD'],
                            input=data, capture_output=True, env=env, timeout=15)
    if result.returncode:
        raise ValueError('The P12 archive could not be opened. Check its password and the APK.')
    validate_pem(result.stdout, openssl)
    return result.stdout


def validate_pem(data, openssl=None):
    openssl = openssl or shutil.which('openssl')
    if not openssl:
        raise RuntimeError('Install OpenSSL before importing the certificate.')
    cert = subprocess.run([openssl, 'x509', '-pubkey', '-noout'], input=data,
                          capture_output=True, timeout=10)
    key = subprocess.run([openssl, 'pkey', '-pubout'], input=data,
                         capture_output=True, timeout=10)
    if cert.returncode or key.returncode or not cert.stdout or cert.stdout != key.stdout:
        raise ValueError('The PEM must contain a client certificate and its matching private key.')


def write_private_pem(data, destination):
    destination = Path(destination).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
    return destination


def device_apk(adb, serial):
    def run(*args, binary=False):
        result = subprocess.run([adb] + (['-s', serial] if serial else []) + list(args),
                                capture_output=True, timeout=45)
        if result.returncode:
            raise RuntimeError('ADB could not read the installed VIDAA app. Check the USB connection and debugging authorization.')
        return result.stdout if binary else result.stdout.decode()
    if not serial:
        devices = [line.split()[0] for line in run('devices').splitlines()[1:]
                   if len(line.split()) >= 2 and line.split()[1] == 'device']
        if len(devices) != 1:
            raise ValueError('Connect one authorized Android phone, or choose one with --serial.')
        serial = devices[0]
    paths = [line.removeprefix('package:').strip() for line in
             run('shell', 'pm', 'path', '--user', '0', PACKAGE).splitlines()]
    path = next((p for p in paths if p.endswith('/base.apk')), None)
    if not path or not re.fullmatch(r'/data/app/[A-Za-z0-9_./~+=-]+\.apk', path):
        raise ValueError('The official VIDAA app base APK could not be located on the phone.')
    data = run('exec-out', 'cat', path, binary=True)
    if len(data) > MAX_APK_BYTES:
        raise ValueError('The installed base APK is unexpectedly large.')
    return io.BytesIO(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--apk', type=Path, help='Your own official VIDAA base APK')
    source.add_argument('--pem', type=Path, help='An already extracted certificate and key PEM')
    source.add_argument('--from-device', action='store_true', help='Read the installed app from an authorized USB phone')
    parser.add_argument('--serial', default=os.environ.get('VIDAA_DEVICE_SERIAL', ''))
    parser.add_argument('--adb', default=os.environ.get('VIDAA_ADB') or shutil.which('adb') or 'adb')
    parser.add_argument('--password-env', default='VIDAA_PKCS12_PASSWORD', help='Environment variable containing the P12 archive password')
    default_config = (Path.home() / 'Library/Application Support/VIDAA Remote' if sys.platform == 'darwin'
                      else Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'vidaa-remote')
    parser.add_argument('--output', type=Path, default=default_config / 'client.pem')
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError('A certificate already exists at that destination. It was preserved. Choose another --output if needed.')
        if args.pem:
            data = args.pem.read_bytes()
            validate_pem(data)
        else:
            apk = device_apk(args.adb, args.serial) if args.from_device else args.apk
            archive = certificate_archive(apk)
            password = os.environ.get(args.password_env)
            if password is None:
                password = getpass.getpass('VIDAA app P12 archive password: ')
            data = decode_archive(archive, password)
        destination = write_private_pem(data, args.output)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 1
    print('Client certificate imported with private file permissions:', destination)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
