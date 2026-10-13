"""Verify certificate pinning at the real TLS/MQTT boundary on localhost."""
import hashlib
import socket
import ssl
import sys
import threading
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import PinnedMQTT, mqtt

from cert_fixture import FIXTURES

class TLSBoundaryTests(unittest.TestCase):
    def exchange(self, fingerprint):
        packets=[]
        server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1);server.settimeout(3)
        port=server.getsockname()[1]
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(FIXTURES/'cert.pem'),str(FIXTURES/'key.pem'))
        def serve():
            try:
                connection,_=server.accept()
                with context.wrap_socket(connection,server_side=True) as tls:
                    tls.settimeout(3)
                    try:
                        packets.append(tls.recv(4096))
                    except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):
                        packets.append(b'')
            finally:server.close()
        worker=threading.Thread(target=serve,daemon=True);worker.start()
        client=PinnedMQTT(mqtt.CallbackAPIVersion.VERSION2,client_id='test-desktop',fingerprint=fingerprint)
        client.username_pw_set('fixture-user','fixture-password')
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);ctx.check_hostname=False;ctx.verify_mode=ssl.CERT_NONE
        client.tls_set_context(ctx)
        failed=False
        try:client.connect('127.0.0.1',port,keepalive=10)
        except ssl.SSLError:failed=True
        finally:
            client.disconnect();worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        return failed, packets

    def test_certificate_change_blocks_mqtt_credentials(self):
        failed,packets=self.exchange('00'*32)
        self.assertTrue(failed)
        self.assertEqual(packets,[b''])

    def test_matching_pin_allows_mqtt_connect(self):
        pem=(FIXTURES/'cert.pem').read_text()
        expected=hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        failed,packets=self.exchange(expected)
        self.assertFalse(failed)
        self.assertIn(b'MQTT',packets[0])
        self.assertIn(b'test-desktop',packets[0])

if __name__=='__main__':unittest.main()
