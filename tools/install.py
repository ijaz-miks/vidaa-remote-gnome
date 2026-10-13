#!/usr/bin/env python3
"""Install VIDAA Remote for the current user, preserving existing installations."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import urllib.request
import venv
import zipfile

SOURCE = Path(__file__).resolve().parents[1]
APP_ID = 'dev.local.VidaaRemote'
PAHO_WHEEL = 'paho_mqtt-2.1.0-py3-none-any.whl'
PAHO_SHA256 = '6db9ba9b34ed5bc6b6e3812718c7e06e2fd7444540df2455d2c51bd58808feee'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-home', type=Path, default=Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))))
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    parser.add_argument('--wheel', type=Path, help='Use a previously downloaded Paho 2.1.0 wheel offline')
    args = parser.parse_args()
    data_home, bin_dir = args.data_home.expanduser().absolute(), args.bin_dir.expanduser().absolute()
    application = data_home / 'vidaa-remote'
    launcher = bin_dir / 'vidaa-remote'
    desktop = data_home / 'applications' / (APP_ID + '.desktop')
    icon = data_home / 'icons/hicolor/scalable/apps' / (APP_ID + '.svg')
    targets = (application, launcher, desktop, icon)
    existing = [str(p) for p in targets if p.exists() or p.is_symlink()]
    if existing:
        parser.error('Existing files were preserved. Choose unused --data-home and --bin-dir locations: ' + ', '.join(existing))
    try:
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        gi.require_version('Secret', '1')
        from gi.repository import Gtk, Adw
        if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 12) or (Adw.get_major_version(), Adw.get_minor_version()) < (1, 5):
            raise RuntimeError('GTK 4.12 and libadwaita 1.5 or newer are required.')
    except (ImportError, ValueError, RuntimeError) as error:
        parser.error('Install the Ubuntu dependencies in the README and run this script with /usr/bin/python3. ' + str(error))
    application.mkdir(parents=True)
    environment = application / '.venv'
    venv.EnvBuilder(system_site_packages=True, with_pip=False).create(environment)
    python = environment / 'bin/python'
    dependencies = application / 'dependencies'
    dependencies.mkdir()
    wheel = dependencies / PAHO_WHEEL
    if args.wheel:
        shutil.copy2(args.wheel, wheel)
    else:
        with urllib.request.urlopen('https://pypi.org/pypi/paho-mqtt/2.1.0/json', timeout=20) as response:
            metadata = json.load(response)
        release = next(item for item in metadata['urls'] if item['filename'] == PAHO_WHEEL)
        if release['digests']['sha256'] != PAHO_SHA256:
            raise RuntimeError('The dependency checksum differs from the pinned release.')
        with urllib.request.urlopen(release['url'], timeout=30) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        with wheel.open('xb') as stream:
            stream.write(data)
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != PAHO_SHA256:
        raise RuntimeError('The Paho wheel failed checksum verification.')
    site = Path(subprocess.check_output([str(python), '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip())
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    for name in ('app.py', 'backend.py', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(SOURCE / name, application / name)
    shutil.copytree(SOURCE / 'licenses', application / 'licenses')
    for target in (launcher, desktop, icon):
        target.parent.mkdir(parents=True, exist_ok=True)
    with launcher.open('x') as stream:
        stream.write('#!/bin/sh\nexec ' + shlex.quote(str(python)) + ' ' + shlex.quote(str(application / 'app.py')) + ' "$@"\n')
    launcher.chmod(0o755)
    entry = (SOURCE / 'data' / (APP_ID + '.desktop.in')).read_text()
    with desktop.open('x') as stream:
        stream.write(entry.replace('@EXEC@', json.dumps(str(launcher)).replace('%', '%%')))
    with icon.open('xb') as stream:
        stream.write((SOURCE / 'data' / (APP_ID + '.svg')).read_bytes())
    updater = shutil.which('update-desktop-database')
    if updater:
        subprocess.run([updater, str(desktop.parent)], check=False)
    print('Installed VIDAA Remote:', launcher)
    print('Import your VIDAA client certificate, then launch VIDAA Remote from GNOME.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
