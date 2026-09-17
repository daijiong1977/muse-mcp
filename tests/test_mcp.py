import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from muse import AuthError, GatewayError
from muse_mcp.client import MuseClient


class FakeGateway:
    def __init__(self, responses=None, history=None, raw=b'{"ok":true}'):
        self.vm_id = "vm-test"
        self.responses = responses or {}
        self.history = list(history or [])
        self.raw = raw
        self.opened = []
        self.requests = []
        self.closed = False

    def call_json(self, method, body=None, query=None, timeout=30):
        if method == "chat.history":
            if self.history:
                value = self.history.pop(0)
                return value() if callable(value) else value
            return {"chat_events": []}
        value = self.responses.get(method, {})
        return value() if callable(value) else value

    def _open(self, method, body=None):
        self.opened.append((method, body))
        return 17

    def request(self, method, path_params=None, body=None, query=None, timeout=30):
        self.requests.append((method, path_params, body, query, timeout))
        return self.raw

    def close(self):
        self.closed = True


def config_for(path):
    return {"cookies_file": str(path), "node_id": "node-test", "aliases": {"instacart": "thread-1"}}


class MCPClientTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.cookie_file = Path(self.tempdir.name) / "cookies.txt"
        self.cookie_file.write_text("hatch_sess=session\n", encoding="utf-8")
        self.gateways = []

    def tearDown(self):
        self.tempdir.cleanup()

    def make_client(self, gateway):
        self.gateways.append(gateway)
        return MuseClient(config_loader=lambda: config_for(self.cookie_file), gateway_factory=lambda *a, **k: gateway)

    def test_threads_filter_and_shape(self):
        gateway = FakeGateway(responses={"sessions.list": {"sessions": [
            {"session_id": "one", "title": "One", "pinned": True, "archived": False, "updated_at_ms": 1700000000000},
            {"session_id": "old", "title": "Old", "pinned": False, "archived": True},
        ]}})
        client = self.make_client(gateway)
        rows = client.threads()
        self.assertEqual([row["session_id"] for row in rows], ["one"])
        self.assertEqual(client.threads(include_archived=True)[1]["archived"], True)
        self.assertEqual(rows[0]["updated_at_ms"], 1700000000000)

    def test_status_reports_connection_state(self):
        gateway = FakeGateway(responses={
            "sessions.list": {"sessions": [{"session_id": "one"}]},
            "chat.unread_count": {"count": 4},
            "identity": {"name": "Muse"},
            "vm.health": {"status": "healthy"},
        })
        client = self.make_client(gateway)
        result = client.status()
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["vm_id"], "vm-test")
        self.assertEqual(result["session_count"], 1)
        self.assertEqual(result["unread_count"], 4)
        self.assertEqual(result["vm"]["health"]["status"], "healthy")

    def test_send_scopes_thread_and_ignores_proactive_event(self):
        proactive = {"seq": 2, "event_name": "message.assistant", "reply_to_message_id": "push", "payload": {"display_text": "background"}}
        reply = {"seq": 3, "event_name": "message.assistant", "payload": {"display_text": "done", "message_id": "m3"}}
        gateway = FakeGateway(history=[{"chat_events": [{"seq": 1}]}, {"chat_events": [proactive, reply]}])
        client = self.make_client(gateway)
        result = client.send("research", thread="instacart", wait_seconds=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reply"]["text"], "done")
        self.assertEqual(gateway.opened[0][1]["session_id"], "thread-1")

    def test_send_timeout_is_structured(self):
        gateway = FakeGateway(history=[{"chat_events": [{"seq": 1}]}])
        client = self.make_client(gateway)
        result = client.send("long job", wait_seconds=0.01)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TIMEOUT")
        self.assertTrue(result["sent"])

    def test_history_preserves_event_metadata(self):
        event = {"seq": 9, "event_name": "message.assistant", "reply_to_message_id": "push", "payload": {"display_text": "news", "message_id": "m9"}}
        gateway = FakeGateway(history=[{"chat_events": [event]}])
        client = self.make_client(gateway)
        rows = client.history(thread="instacart", limit=5)
        self.assertEqual(rows[0]["reply_to_message_id"], "push")
        self.assertEqual(rows[0]["raw"]["seq"], 9)

    def test_feed_and_raw_redact_secrets(self):
        feed = {"days": [{"local_date": "2026-09-17", "editions": [{"kind": "news", "units": [{"unit_id": "u1", "title": "T", "content": "C", "access_token": "secret"}]}]}]}
        gateway = FakeGateway(responses={"feed.list": feed}, raw=b'{"access_token":"secret","title":"safe"}')
        client = self.make_client(gateway)
        self.assertEqual(client.feed()[0]["id"], "u1")
        result = client.raw("feed.list", body={"x": 1}, path_params={"id": "one"}, query_params={"limit": 2}, timeout=7)
        self.assertEqual(result["result"]["access_token"], "[REDACTED]")
        self.assertEqual(gateway.requests[0], ("feed.list", {"id": "one"}, {"x": 1}, {"limit": 2}, 7.0))
        self.assertEqual(client.raw("not-a-route")["error"]["code"], "UNKNOWN_ROUTE")

    def test_auth_and_vm_failures_are_actionable(self):
        auth_client = MuseClient(config_loader=lambda: config_for(self.cookie_file), gateway_factory=lambda *a, **k: (_ for _ in ()).throw(AuthError("token-secret")))
        auth = auth_client.status()
        self.assertEqual(auth["error"]["code"], "AUTH_REQUIRED")
        self.assertNotIn("token-secret", json.dumps(auth))

        vm_client = MuseClient(config_loader=lambda: config_for(self.cookie_file), gateway_factory=lambda *a, **k: (_ for _ in ()).throw(GatewayError(-1, "private")))
        self.assertEqual(vm_client.status()["error"]["code"], "VM_UNAVAILABLE")

    def test_gateway_operations_are_serialized(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        class SlowGateway(FakeGateway):
            def call_json(self, method, body=None, query=None, timeout=30):
                nonlocal active, maximum
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with guard:
                    active -= 1
                return {"sessions": []} if method == "sessions.list" else {}

        def factory(*args, **kwargs):
            return SlowGateway()

        client = MuseClient(config_loader=lambda: config_for(self.cookie_file), gateway_factory=factory)
        threads = [threading.Thread(target=client.threads) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(maximum, 1)


if __name__ == "__main__":
    unittest.main()
