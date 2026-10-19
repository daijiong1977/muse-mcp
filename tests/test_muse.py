import unittest
import threading
from types import SimpleNamespace
from unittest.mock import patch

import muse
from muse import Gateway, GatewayError, Keypair, VMUnavailableError


class FakeWebSocket:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def connect(self, url, **kwargs):
        self.events.append(("connect", url))

    def send_bytes(self, payload):
        self.events.append(("send_bytes", payload))

    def recv(self):
        self.events.append(("recv",))
        return b"server-handshake", None

    def close(self):
        self.closed = True


class FakeNoise:
    def __init__(self, events):
        self.events = events
        self.started = False
        self.handshake_finished = True

    def set_as_initiator(self):
        self.events.append(("set_as_initiator",))

    def set_keypair_from_private_bytes(self, keypair, private_key):
        self.events.append(("set_keypair", keypair, private_key))

    def start_handshake(self):
        self.started = True
        self.events.append(("start_handshake",))

    def write_message(self, payload):
        if not self.started:
            raise AssertionError("write_message called before start_handshake")
        self.events.append(("write_message", payload))
        return b"client-handshake"

    def read_message(self, payload):
        self.events.append(("read_message", payload))


class NoiseFactory:
    def __init__(self, events):
        self.events = events
        self.instance = None

    def from_name(self, name):
        self.events.append(("from_name", name))
        self.instance = FakeNoise(self.events)
        return self.instance


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload
        self.text = ""

    def json(self):
        return self.payload


class MuseTransportTests(unittest.TestCase):
    def test_outbound_transport_chunks_reassemble_exactly(self):
        for size in (0, 100, 65480, 65489, 700000):
            with self.subTest(size=size):
                gateway = Gateway.__new__(Gateway)
                sent = []
                gateway.ws = SimpleNamespace(send_bytes=sent.append)
                gateway.noise = SimpleNamespace(encrypt=lambda payload: payload)
                gateway._send_lock = threading.Lock()
                original = b"x" * size
                gateway._send_envelope(0, original)
                frames = []
                for payload in sent:
                    frame = muse.NoiseTransportFrame()
                    frame.ParseFromString(payload)
                    frames.append(frame)
                    self.assertLessEqual(len(payload) + 16, 65535)
                    self.assertLessEqual(len(frame.payload), 65489)
                self.assertEqual([f.chunk_index for f in frames], list(range(len(frames))))
                self.assertEqual({f.total_chunks for f in frames}, {len(frames)})
                self.assertEqual(len({f.chunk_id for f in frames}), 1)
                outer = muse.ServiceRequest()
                outer.ParseFromString(b"".join(f.payload for f in frames))
                self.assertEqual(outer.payload, original)
                self.assertEqual(outer.service, 0)

    def test_oversized_transport_does_not_send_partial_request(self):
        gateway = Gateway.__new__(Gateway)
        sent = []
        gateway.ws = SimpleNamespace(send_bytes=sent.append)
        gateway._send_lock = threading.Lock()
        with self.assertRaisesRegex(ValueError, "256"):
            gateway._send_envelope(0, b"x" * (65489 * 256))
        self.assertEqual(sent, [])

    def test_noise_xx_initialization_order(self):
        events = []
        noise_factory = NoiseFactory(events)
        websocket_factory = lambda: FakeWebSocket(events)
        with patch.object(muse, "rq", object()), patch.object(muse, "WebSocket", websocket_factory), patch.object(muse, "NoiseConnection", noise_factory):
            gateway = Gateway("cookies", vm_id="vm", access_token="access", hatch_token="hatch")
            gateway.close()

        names = [event[0] for event in events]
        self.assertLess(names.index("set_as_initiator"), names.index("set_keypair"))
        self.assertLess(names.index("set_keypair"), names.index("start_handshake"))
        self.assertLess(names.index("start_handshake"), names.index("write_message"))
        self.assertEqual([name for name in names if name == "write_message"], ["write_message", "write_message"])
        key_event = next(event for event in events if event[0] == "set_keypair")
        self.assertIs(key_event[1], Keypair.STATIC)
        self.assertEqual(len(key_event[2]), 32)

    def test_known_vm_resolution_failure_is_specific(self):
        response = Response({
            "status": "unavailable",
            "vm_resolution_issue": {"kind": "retryable"},
        })
        fake_requests = SimpleNamespace(get=lambda *args, **kwargs: response)
        with patch.object(muse, "rq", fake_requests), patch.object(muse, "WebSocket", object()):
            with self.assertRaises(VMUnavailableError):
                muse.fetch_session_info("cookies")

    def test_unknown_session_shape_is_gateway_failure(self):
        response = Response({"status": "unavailable", "vm_resolution_issue": {"kind": "permanent"}})
        fake_requests = SimpleNamespace(get=lambda *args, **kwargs: response)
        with patch.object(muse, "rq", fake_requests), patch.object(muse, "WebSocket", object()):
            with self.assertRaises(GatewayError) as caught:
                muse.fetch_session_info("cookies")
        self.assertIs(type(caught.exception), GatewayError)


if __name__ == "__main__":
    unittest.main()
