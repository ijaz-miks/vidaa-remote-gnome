#!/usr/bin/env python3
"""Build a native macOS app in a new bundle, preserving existing installations."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile

SOURCE = Path(__file__).resolve().parents[1]
WHEEL = 'paho_mqtt-2.1.0-py3-none-any.whl'
SHA256 = '6db9ba9b34ed5bc6b6e3812718c7e06e2fd7444540df2455d2c51bd58808feee'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=Path.home() / 'Applications/VIDAA Remote.app')
    parser.add_argument('--wheel', type=Path, help='Use a pinned dependency wheel offline')
    parser.add_argument('--python', type=Path, default=Path(sys.executable).resolve())
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Build this app on macOS with Xcode Command Line Tools installed.')
    destination = args.destination.expanduser().absolute()
    if os.path.lexists(destination):
        parser.error('Existing bundle preserved. Choose an unused --destination.')
    if not shutil.which('xcrun') or not shutil.which('codesign'):
        parser.error('Xcode Command Line Tools are required.')
    python = args.python.expanduser().resolve()
    subprocess.run([str(python), '-c', 'import ssl,sys; assert sys.version_info >= (3,9)'], check=True)
    contents = destination / 'Contents'
    binary = contents / 'MacOS/VidaaRemote'
    resources = contents / 'Resources'
    resources.mkdir(parents=True)
    binary.parent.mkdir()
    dependencies = resources / 'dependencies'
    dependencies.mkdir()
    wheel = dependencies / WHEEL
    if args.wheel:
        shutil.copy2(args.wheel, wheel)
    else:
        with urllib.request.urlopen('https://pypi.org/pypi/paho-mqtt/2.1.0/json', timeout=20) as response:
            release = next(item for item in json.load(response)['urls'] if item['filename'] == WHEEL)
        if release['digests']['sha256'] != SHA256:
            raise RuntimeError('The dependency checksum differs from the pinned release.')
        with urllib.request.urlopen(release['url'], timeout=30) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        with wheel.open('xb') as stream:
            stream.write(data)
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError('The Paho wheel failed checksum verification.')
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(resources / 'pythonlib')
    for path in (SOURCE / 'backend.py', SOURCE / 'macos/worker.py', SOURCE / 'tools/import_certificate.py',
                 SOURCE / 'LICENSE', SOURCE / 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(path, resources / path.name)
    shutil.copytree(SOURCE / 'licenses', resources / 'licenses')
    (resources / 'runtime.json').write_text(json.dumps({'python': str(python)}) + '\n')
    info = {'CFBundleIdentifier': 'dev.local.VidaaRemoteMac', 'CFBundleName': 'VIDAA Remote',
            'CFBundleDisplayName': 'VIDAA Remote', 'CFBundleExecutable': 'VidaaRemote',
            'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '0.2.0',
            'CFBundleVersion': '2', 'LSMinimumSystemVersion': '13.0',
            'CFBundleIconFile': 'VidaaRemote.icns',
            'NSHighResolutionCapable': True, 'LSApplicationCategoryType': 'public.app-category.utilities',
            'NSLocalNetworkUsageDescription': 'Find and control your VIDAA TV on your local network.',
            'NSPrincipalClass': 'NSApplication'}
    with (contents / 'Info.plist').open('wb') as stream:
        plistlib.dump(info, stream)
    subprocess.run(['xcrun', 'swiftc', '-swift-version', '5', '-parse-as-library', '-O',
                    '-target', platform.machine() + '-apple-macosx13.0',
                    str(SOURCE / 'macos/VidaaRemote.swift'), '-o', str(binary)], check=True)
    iconset = dependencies / 'VidaaRemote.iconset'
    subprocess.run(['xcrun', 'swift', str(SOURCE / 'macos/icon.swift'), str(iconset)], check=True)
    subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(resources / 'VidaaRemote.icns')], check=True)
    subprocess.run(['codesign', '--sign', '-', '--identifier', info['CFBundleIdentifier'], str(destination)], check=True)
    subprocess.run(['codesign', '--verify', '--strict', str(destination)], check=True)
    print('Installed VIDAA Remote:', destination)
    print('Import your client PEM, then open the app and pair this Mac using the TV code.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
