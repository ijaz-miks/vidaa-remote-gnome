"""Private JSON pipe bridge between the native Mac app and the shared TV backend.

Keychain values travel only through anonymous parent/child pipes. The optional
user-only command FIFO accepts remote actions, never Keychain responses.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import stat
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'pythonlib'))
sys.path.insert(0, str(ROOT))
import backend


class Bridge:
    def __init__(self, output=None):
        self.output = output or sys.stdout
        self.lock = threading.Lock()
        self.pending = {}

    def send(self, kind, value):
        with self.lock:
            self.output.write(json.dumps({'event': kind, 'value': value}) + '\n')
            self.output.flush()

    def request(self, method, account, value=None):
        ident = str(uuid.uuid4())
        entry = {'event': threading.Event()}
        with self.lock:
            self.pending[ident] = entry
        try:
            self.send('keychain', {'id': ident, 'method': method, 'account': account, 'data': value})
            if not entry['event'].wait(60):
                raise RuntimeError('macOS Keychain did not answer. Check for a Keychain prompt on the Mac.')
            response = entry['response']
            if response.get('error'):
                raise RuntimeError('macOS Keychain could not access this app pairing.')
            return response.get('value')
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def reply(self, response):
        with self.lock:
            entry = self.pending.get(response.get('reply'))
            if entry:
                entry['response'] = response
                entry['event'].set()


def token_store(bridge):
    class MacTokens:
        def load(self, tv):
            return bridge.request('load', 'tv:' + (tv['mac'] or tv['host']))

        def save(self, tv, value):
            return bool(bridge.request('save', 'tv:' + (tv['mac'] or tv['host']), value))
    return MacTokens


class Worker:
    METHODS = {'connect', 'disconnect', 'discover', 'manual', 'pair', 'pin',
               'key', 'volume', 'source', 'app', 'refresh', 'wake', 'import-certificate'}

    def __init__(self, bridge):
        self.bridge = bridge
        self.settings = backend.Settings()
        self.controller = None
        self.epoch = 0
        self.actions = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mac-actions')
        self.summary = {'state': 'offline', 'sources': 0, 'apps': 0, 'paired': False}
        backend.Tokens = token_store(bridge)
        self.emit('device', self.settings.data.get('tv', {}))

    def emit(self, kind, value):
        self.bridge.send(kind, value)
        if kind == 'status':
            self.summary['state'] = value.get('state', 'offline')
            self.summary.pop('error', None)
            if self.summary['state'] == 'ready':
                self.summary['paired'] = True
        elif kind in ('sources', 'apps'):
            self.summary[kind] = len(value)
        elif kind == 'paired':
            self.summary['paired'] = bool(value.get('saved'))
        elif kind == 'volume':
            self.summary['volume'] = value
        elif kind == 'error':
            self.summary['error'] = str(value).replace('GNOME Keyring', 'macOS Keychain')
            if not self.controller or not self.controller.ready:
                self.summary['state'] = 'error'
        # Health information contains no tokens, client identity, or TV address.
        path = backend.CONFIG_DIR / 'status.json'
        with self.bridge.lock:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(self.summary, stream)

    def dispatch(self, command):
        if command.get('method') not in self.METHODS:
            self.bridge.send('error', 'Unknown remote action.')
            return
        self.actions.submit(self.perform, command)

    def perform(self, command):
        try:
            method, args = command['method'], command.get('args', {})
            if method == 'discover':
                self.emit('discovered', backend.discover())
            elif method == 'manual':
                tv = self.settings.set_tv(backend.manual_device(args['host']))
                self.emit('device', tv)
            elif method == 'import-certificate':
                import import_certificate
                data = Path(args['path']).read_bytes()
                if len(data) > 1048576:
                    raise ValueError('The certificate file is too large.')
                import_certificate.validate_pem(data)
                import_certificate.write_private_pem(data, backend.CERT_FILE)
                self.emit('certificate-imported', {})
            elif method == 'connect':
                self.stop()
                if not self.settings.data.get('tv', {}).get('host'):
                    raise ValueError('Find your TV or enter its address first.')
                epoch = self.epoch
                self.controller = backend.DirectController(self.settings,
                    lambda kind, value: self.emit(kind, value) if epoch == self.epoch else None)
                self.controller.connect()
            elif method == 'disconnect':
                self.stop()
                self.emit('status', {'state': 'offline', 'text': 'Disconnected from TV'})
            elif method == 'wake':
                backend.wake(self.settings.data['tv'])
            else:
                controller = self.controller
                if not controller:
                    raise ConnectionError('Connect to the TV first.')
                if method == 'pair':
                    controller.pair()
                elif method == 'pin':
                    controller.submit_pin(args['pin'])
                elif method == 'key':
                    controller.send_key(args['key'])
                elif method == 'volume':
                    controller.set_volume(args['value'])
                elif method == 'source':
                    controller.set_source(args['source'])
                elif method == 'app':
                    controller.launch_app(args['app'])
                elif method == 'refresh':
                    controller.refresh()
        except Exception as error:
            message = str(error).replace('GNOME Keyring', 'macOS Keychain')
            if sys.platform == 'darwin' and isinstance(error, OSError) and error.errno == 65:
                message = 'Cannot reach the TV. Check its address and Mac Local Network permission, then reconnect.'
            self.emit('error', message)

    def stop(self):
        self.epoch += 1
        if self.controller:
            self.controller.close()
            self.controller = None

    def command_fifo(self):
        path = backend.CONFIG_DIR / 'commands.fifo'
        if os.path.lexists(path):
            info = path.lstat()
            if not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('The local command path is not a FIFO owned by this user.')
        else:
            os.mkfifo(path, 0o600)
        os.chmod(path, 0o600)
        def receive():
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            with os.fdopen(fd, 'r') as stream:
                for line in stream:
                    if len(line) > 65536:
                        continue
                    try:
                        command = json.loads(line)
                        if isinstance(command, dict):
                            self.dispatch(command)
                    except (ValueError, TypeError):
                        pass
        threading.Thread(target=receive, daemon=True, name='mac-local-commands').start()


def main():
    worker = Worker(Bridge())
    if '--automation' in sys.argv:
        worker.command_fifo()
    try:
        for line in sys.stdin:
            if len(line) > 4194304:
                continue
            try:
                command = json.loads(line)
                if 'reply' in command:
                    worker.bridge.reply(command)
                else:
                    worker.dispatch(command)
            except (ValueError, TypeError):
                worker.bridge.send('error', 'Invalid local command.')
    finally:
        worker.stop()
        worker.actions.shutdown(wait=False)


if __name__ == '__main__':
    main()
