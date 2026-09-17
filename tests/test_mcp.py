import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import cli

from muse import AuthError, GatewayError, VMUnavailableError
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


def config_for(path, enable_raw=False):
    return {
        "cookies_file": str(path),
        "node_id": "node-test",
        "aliases": {"instacart": "thread-1"},
        "enable_raw": enable_raw,
    }


class MCPClientTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.cookie_file = Path(self.tempdir.name) / "cookies.txt"
        self.cookie_file.write_text("hatch_sess=session\n", encoding="utf-8")
        self.gateways = []

    def tearDown(self):
        self.tempdir.cleanup()

    def make_client(self, gateway, enable_raw=False):
        self.gateways.append(gateway)
        return MuseClient(
            config_loader=lambda: config_for(self.cookie_file, enable_raw=enable_raw),
            gateway_factory=lambda *a, **k: gateway,
        )


    def test_auth_export_reports_missing_agent_browser(self):
        stderr = io.StringIO()
        with patch.object(cli.shutil, "which", return_value=None), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exited:
                cli.cmd_auth_export(None)
        self.assertEqual(exited.exception.code, 1)
        self.assertIn("npm i -g agent-browser", stderr.getvalue())

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

    def test_send_invalid_thread_is_structured(self):
        class InvalidThreadGateway(FakeGateway):
            def call_json(self, method, body=None, query=None, timeout=30):
                if method == "chat.history":
                    raise GatewayError(404, "missing thread")
                return super().call_json(method, body=body, query=query, timeout=timeout)

        gateway = InvalidThreadGateway()
        client = self.make_client(gateway)
        result = client.send("research", thread="missing", fire_and_forget=True)
        self.assertEqual(result["error"]["code"], "INVALID_THREAD")
        self.assertEqual(gateway.opened, [])

    def test_send_poll_gateway_failure_is_not_timeout(self):
        class PollFailureGateway(FakeGateway):
            def __init__(self):
                super().__init__()
                self.history_calls = 0

            def call_json(self, method, body=None, query=None, timeout=30):
                if method == "chat.history":
                    self.history_calls += 1
                    if self.history_calls == 1:
                        return {"chat_events": [{"seq": 1}]}
                    raise GatewayError(-1, "stream reset")
                return super().call_json(method, body=body, query=query, timeout=timeout)

        gateway = PollFailureGateway()
        client = self.make_client(gateway)
        result = client.send("research", wait_seconds=10)
        self.assertEqual(result["error"]["code"], "GATEWAY_ERROR")

    def test_send_timeout_is_structured(self):
        gateway = FakeGateway(history=[{"chat_events": [{"seq": 1}]}])
        client = self.make_client(gateway)
        result = client.send("long job", wait_seconds=0.01)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TIMEOUT")
        self.assertTrue(result["sent"])
        self.assertFalse(result["acknowledged"])
        self.assertIn("dispatched", result["error"]["message"])

    def test_fire_and_forget_confirms_send_and_closes_gateway(self):
        gateway = FakeGateway(history=[{"chat_events": [{"seq": 1}]}])
        client = self.make_client(gateway)
        result = client.send("prepare this", fire_and_forget=True)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["sent"], True)
        self.assertEqual(result["waiting"], False)
        self.assertFalse(result["acknowledged"])
        self.assertIn("dispatched", result["note"])
        self.assertTrue(gateway.closed)

    def test_arbitrary_raw_content_is_not_claimed_secret_free(self):
        gateway = FakeGateway(raw=b"file contents: account password is in this document")
        client = self.make_client(gateway, enable_raw=True)
        with patch.dict(os.environ, {"MUSE_MCP_ENABLE_RAW": "1"}):
            result = client.raw("feed.list")
        self.assertIn("file contents", result["result"])
        self.assertIn("password is in this document", result["result"])

    def test_history_preserves_event_metadata(self):
        event = {"seq": 9, "event_name": "message.assistant", "reply_to_message_id": "push", "payload": {"display_text": "news", "message_id": "m9"}}
        gateway = FakeGateway(history=[{"chat_events": [event]}])
        client = self.make_client(gateway)
        rows = client.history(thread="instacart", limit=5)
        self.assertEqual(rows[0]["reply_to_message_id"], "push")
        self.assertEqual(rows[0]["raw"]["seq"], 9)

    def test_feed_raw_redaction_and_opt_in(self):
        feed = {"days": [{"local_date": "2026-09-17", "editions": [{"kind": "news", "units": [{"unit_id": "u1", "title": "T", "content": "C", "access_token": "secret"}]}]}]}
        gateway = FakeGateway(responses={"feed.list": feed}, raw=b'{"access_token":"secret","title":"safe"}')
        disabled = self.make_client(gateway)
        with patch.dict(os.environ, {"MUSE_MCP_ENABLE_RAW": "0"}):
            self.assertEqual(disabled.raw("feed.list")["error"]["code"], "RAW_DISABLED")

        client = self.make_client(gateway, enable_raw=True)
        self.assertEqual(client.feed()[0]["id"], "u1")
        with patch.dict(os.environ, {"MUSE_MCP_ENABLE_RAW": "1"}):
            result = client.raw("feed.list", body={"x": 1}, path_params={"id": "one"}, query_params={"limit": 2}, timeout=7)
            self.assertEqual(result["result"]["access_token"], "[REDACTED]")
            self.assertEqual(gateway.requests[0], ("feed.list", {"id": "one"}, {"x": 1}, {"limit": 2}, 7.0))
            self.assertEqual(client.raw("not-a-route")["error"]["code"], "UNKNOWN_ROUTE")

    def test_auth_and_gateway_failures_are_actionable(self):
        auth_client = MuseClient(config_loader=lambda: config_for(self.cookie_file), gateway_factory=lambda *a, **k: (_ for _ in ()).throw(AuthError("token-secret")))
        auth = auth_client.status()
        self.assertEqual(auth["error"]["code"], "AUTH_REQUIRED")
        self.assertNotIn("token-secret", json.dumps(auth))

        unavailable = MuseClient(config_loader=lambda: config_for(self.cookie_file), gateway_factory=lambda *a, **k: (_ for _ in ()).throw(VMUnavailableError(-1, "private")))
        self.assertEqual(unavailable.status()["error"]["code"], "VM_UNAVAILABLE")


        bearer_gateway = FakeGateway(raw=b"Authorization: Bearer topsecret; api_key=key-secret")
        bearer_client = self.make_client(bearer_gateway, enable_raw=True)
        with patch.dict(os.environ, {"MUSE_MCP_ENABLE_RAW": "1"}):
            bearer = bearer_client.raw("feed.list")
        self.assertNotIn("topsecret", bearer["result"])
        self.assertNotIn("key-secret", bearer["result"])
        self.assertIn("[REDACTED]", bearer["result"])
        for failure in (GatewayError(-1, "stream reset"), GatewayError(-1, "non-JSON"), GatewayError(500, "server")):
            failed = MuseClient(config_loader=lambda: config_for(self.cookie_file), gateway_factory=lambda *a, failure=failure, **k: (_ for _ in ()).throw(failure))
            self.assertEqual(failed.status()["error"]["code"], "GATEWAY_ERROR")

        class InvalidThreadGateway(FakeGateway):
            def call_json(self, method, body=None, query=None, timeout=30):
                if method == "chat.history":
                    raise GatewayError(404, "missing thread")
                return super().call_json(method, body=body, query=query, timeout=timeout)

        invalid = self.make_client(InvalidThreadGateway())
        self.assertEqual(invalid.history(thread="missing")["error"]["code"], "INVALID_THREAD")

    def test_independent_gateway_operations_overlap(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        class SlowGateway(FakeGateway):
            def call_json(self, method, body=None, query=None, timeout=30):
                nonlocal active, maximum
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.05)
                with guard:
                    active -= 1
                return {"sessions": []} if method == "sessions.list" else {"chat_events": []}

        client = MuseClient(config_loader=lambda: config_for(self.cookie_file), gateway_factory=lambda *a, **k: SlowGateway())
        workers = [threading.Thread(target=client.threads) for _ in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertGreaterEqual(maximum, 2)

    def test_long_send_does_not_block_history(self):
        send_started = threading.Event()
        release_send = threading.Event()
        factory_calls = 0
        factory_guard = threading.Lock()

        class BlockingSendGateway(FakeGateway):
            def call_json(self, method, body=None, query=None, timeout=30):
                if method == "chat.history":
                    send_started.set()
                    release_send.wait(2)
                    return {"chat_events": [{"seq": 1}]}
                return super().call_json(method, body=body, query=query, timeout=timeout)

        def factory(*args, **kwargs):
            nonlocal factory_calls
            with factory_guard:
                factory_calls += 1
                return BlockingSendGateway() if factory_calls == 1 else FakeGateway()

        client = MuseClient(config_loader=lambda: config_for(self.cookie_file), gateway_factory=factory)
        sender = threading.Thread(target=lambda: client.send("long job", fire_and_forget=True))
        sender.start()
        self.assertTrue(send_started.wait(1))
        history_result = []
        reader = threading.Thread(target=lambda: history_result.append(client.history(limit=1)))
        reader.start()
        reader.join(1)
        self.assertFalse(reader.is_alive())
        self.assertEqual(history_result, [[]])
        release_send.set()
        sender.join(2)
        self.assertFalse(sender.is_alive())


if __name__ == "__main__":
    unittest.main()
