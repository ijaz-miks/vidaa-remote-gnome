"""Exercise desktop pairing and token reconnection against a local TLS broker."""
import hashlib
import json
import socket
import ssl
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend

from cert_fixture import FIXTURES


def field(value):
    value = value.encode()
    return len(value).to_bytes(2, 'big') + value


def packet(header, body):
    encoded = bytearray()
    length = len(body)
    while True:
        value = length % 128
        length //= 128
        encoded.append(value | (128 if length else 0))
        if not length:
            return bytes([header]) + encoded + body


def receive(sock):
    def exact(count):
        result = b''
        while len(result) < count:
            part = sock.recv(count - len(result))
            if not part:
                raise EOFError()
            result += part
        return result
    header = exact(1)[0]
    length, scale = 0, 1
    for _ in range(4):
        value = exact(1)[0]
        length += (value & 127) * scale
        if not value & 128:
            return header, exact(length)
        scale *= 128
    raise ValueError('Invalid fixture packet length')


def connect_fields(body):
    position, values = 10, []
    for _ in range(3):
        length = int.from_bytes(body[position:position + 2], 'big')
        position += 2
        values.append(body[position:position + length].decode())
        position += length
    return values


class PairingSessionTests(unittest.TestCase):
    def test_own_token_is_used_on_new_connection_before_controls_work(self):
        token = 'local-fixture-access-token'
        uuid = '02:11:22:33:44:55'
        pem = (FIXTURES / 'cert.pem').read_text()
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        settings = SimpleNamespace(data={'uuid': uuid, 'tv': {
            'host': '127.0.0.1', 'mac': 'fixture-tv', 'name': 'Fixture TV',
            'protocol': 3290, 'brand': 'his', 'cert_sha256': fingerprint}}, set_tv=lambda tv: None)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        listener.settimeout(5)
        port = listener.getsockname()[1]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(FIXTURES / 'cert.pem'), str(FIXTURES / 'key.pem'))
        subscriptions, pin_needed, ready, volume = [threading.Event() for _ in range(4)]
        logins, errors = [], []

        class MemoryTokens:
            value = None
            def load(self, tv):
                return self.value
            def save(self, tv, value):
                MemoryTokens.value = dict(value)
                return True

        class FixtureMQTT(backend.PinnedMQTT):
            def connect(self, host, mqtt_port, keepalive=30):
                return super().connect(host, port, keepalive)

        def publish(sock, topic, value):
            sock.sendall(packet(0x30, field(topic) + json.dumps(value).encode()))

        def serve():
            try:
                for session in range(2):
                    raw, _ = listener.accept()
                    with context.wrap_socket(raw, server_side=True) as sock:
                        sock.settimeout(5)
                        header, body = receive(sock)
                        if header != 0x10:
                            raise AssertionError('Expected MQTT CONNECT')
                        cid, username, password = connect_fields(body)
                        logins.append((cid, username, password))
                        sock.sendall(b'\x20\x02\x00\x00')
                        while True:
                            try:
                                header, body = receive(sock)
                            except EOFError:
                                break
                            if header >> 4 == 14:
                                break
                            if header >> 4 == 8:
                                pos, reasons = 2, []
                                while pos < len(body):
                                    size = int.from_bytes(body[pos:pos + 2], 'big')
                                    pos += 2
                                    topic = body[pos:pos + size].decode()
                                    pos += size + 1
                                    reasons.append(1 if session or 'authentication' in topic or
                                                   'tokenissuance' in topic or 'vidaa_app_connect' in topic else 128)
                                sock.sendall(packet(0x90, body[:2] + bytes(reasons)))
                                subscriptions.set()
                            elif header >> 4 == 3:
                                size = int.from_bytes(body[:2], 'big')
                                topic, payload = body[2:2 + size].decode(), body[2 + size:]
                                base = f'/remoteapp/mobile/{cid}'
                                if topic.endswith('/actions/vidaa_app_connect'):
                                    publish(sock, base + '/ui_service/data/authentication', {})
                                elif topic.endswith('/actions/authenticationcode'):
                                    if json.loads(payload)['authNum'] != 123:
                                        raise AssertionError('PIN was not an integer preserving leading zero input')
                                    publish(sock, base + '/ui_service/data/authenticationcode', {'result': 1})
                                elif topic.endswith('/data/gettoken'):
                                    publish(sock, base + '/platform_service/data/tokenissuance', {
                                        'accesstoken': token, 'refreshtoken': 'local-fixture-refresh-token',
                                        'accesstoken_time': str(int(time.time())), 'accesstoken_duration_day': 7})
                                elif topic.endswith('/actions/getvolume'):
                                    publish(sock, '/remoteapp/mobile/broadcast/platform_service/actions/volumechange',
                                            {'volume_type': 0, 'volume_value': 17})
                            elif header >> 4 == 12:
                                sock.sendall(b'\xd0\x00')
            except Exception as error:
                errors.append(error)
            finally:
                listener.close()

        def emit(kind, value):
            if kind == 'pin-needed':
                pin_needed.set()
            elif kind == 'status' and value['state'] == 'ready':
                ready.set()
            elif kind == 'volume' and value == 17:
                volume.set()

        worker = threading.Thread(target=serve, daemon=True)
        controller = backend.DirectController(settings, emit)
        with patch.object(backend, 'Tokens', MemoryTokens), \
             patch.object(backend, 'PinnedMQTT', FixtureMQTT), \
             patch.object(backend, 'CERT_FILE', FIXTURES / 'client.pem'):
            worker.start()
            try:
                controller.connect()
                self.assertTrue(subscriptions.wait(3))
                self.assertFalse(controller.ready)
                controller.pair()
                self.assertTrue(pin_needed.wait(3))
                controller.submit_pin('0123')
                self.assertTrue(ready.wait(3))
                self.assertTrue(volume.wait(3))
                self.assertEqual(MemoryTokens.value['uuid'], uuid)
                self.assertEqual(len(logins), 2)
                self.assertEqual(logins[0][:2], logins[1][:2])
                self.assertNotEqual(logins[0][2], token)
                self.assertEqual(logins[1][2], token)
            finally:
                controller.close()
                worker.join(timeout=6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
