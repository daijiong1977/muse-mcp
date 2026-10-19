import base64
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

import cli
from muse_mcp.attachments import MAX_IMAGE_BYTES, build_message_items
from muse_mcp.client import MuseClient
from muse_mcp.cli import main
from test_mcp import FakeGateway, config_for


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9S8AAAAASUVORK5CYII="
)


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.image = self.root / "参考图 with spaces.jpg"
        self.image.write_bytes(PNG)
        self.cookie = self.root / "cookies.txt"
        self.cookie.write_text("hatch_sess=test")

    def test_image_bytes_mime_and_order_match_web_client(self):
        second = self.root / "second.png"
        second.write_bytes(PNG)
        items = build_message_items("compare", [str(self.image), str(second)])
        self.assertEqual([item["type"] for item in items], ["image", "image", "text"])
        self.assertEqual(items[0]["mime_type"], "image/png")
        self.assertEqual(base64.b64decode(items[0]["data_base64"]), PNG)
        self.assertEqual(items[0]["filename"], self.image.name)
        self.assertEqual(items[1]["filename"], second.name)
        self.assertEqual(items[2], {"type": "text", "text": "compare"})

    def test_supported_signatures_and_image_only_message(self):
        for contents, mime in [(b"\xff\xd8\xffsample", "image/jpeg"),
                               (b"GIF89asample", "image/gif"),
                               (b"RIFF\0\0\0\0WEBPsample", "image/webp")]:
            with self.subTest(mime=mime):
                self.image.write_bytes(contents)
                items = build_message_items("", [str(self.image)])
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["mime_type"], mime)

    def test_text_only_is_unchanged(self):
        self.assertEqual(build_message_items("hello"), [{"type": "text", "text": "hello"}])

    def test_invalid_inputs_rejected_before_gateway_creation(self):
        factory = Mock()
        client = MuseClient(config_loader=Mock(), gateway_factory=factory)
        for message, images in [("", None), (None, None), ("hi", "path.png"),
                                ("hi", [None]), ("hi", [""]),
                                ("hi", [str(self.root)]),
                                ("hi", [str(self.root / "missing.png")]),
                                ("hi", [str(self.image)] * 11),
                                ("hi", ["https://example.com/image.png"])]:
            with self.subTest(message=message, images=images):
                result = client.send(message, images=images)
                self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        factory.assert_not_called()
        client._config_loader.assert_not_called()

    def test_empty_or_non_image_files_cannot_be_uploaded(self):
        for data in (b"", b"a private text document", b"RIFFnot a WebP"):
            self.image.write_bytes(data)
            with self.assertRaises(ValueError):
                build_message_items("describe", [str(self.image)])

    def test_total_size_is_bounded_before_base64_encoding(self):
        with self.image.open("wb") as output:
            output.write(PNG)
            output.truncate(MAX_IMAGE_BYTES + 1)
        with patch("muse_mcp.attachments.base64.b64encode") as encode:
            with self.assertRaisesRegex(ValueError, "10 MiB"):
                build_message_items("describe", [str(self.image)])
            encode.assert_not_called()
        with self.image.open("wb") as output:
            output.write(PNG)
            output.truncate(MAX_IMAGE_BYTES // 2 + 1)
        with self.assertRaisesRegex(ValueError, "10 MiB"):
            build_message_items("describe", [str(self.image), str(self.image)])

    def test_mcp_send_preserves_thread_and_encodes_images(self):
        reply = {"seq": 2, "event_name": "message.assistant", "payload": {"display_text": "a picture"}}
        gateway = FakeGateway(history=[{"chat_events": [{"seq": 1}]}, {"chat_events": [reply]}])
        client = MuseClient(config_loader=lambda: config_for(self.cookie), gateway_factory=lambda *a, **k: gateway)
        result = client.send("describe", images=[str(self.image)], thread="instacart", wait_seconds=1)
        self.assertTrue(result["ok"])
        self.assertEqual(gateway.opened[0][1]["session_id"], "thread-1")
        self.assertEqual(base64.b64decode(gateway.opened[0][1]["items"][0]["data_base64"]), PNG)
        self.assertNotIn("data_base64", json.dumps(result))
        self.assertTrue(gateway.closed)

    def test_cli_repeatable_option_and_no_wait_output(self):
        gateway = FakeGateway()
        stdout = io.StringIO()
        with patch.object(cli, "connect", return_value=gateway), patch.object(cli, "load_config", return_value={"node_id": "test"}), redirect_stdout(stdout):
            main(["send", "compare", "--image", str(self.image), "--image", str(self.image), "--thread", "thread-2", "--wait", "0"])
        params = gateway.opened[0][1]
        self.assertEqual(params["session_id"], "thread-2")
        self.assertEqual([item["type"] for item in params["items"]], ["image", "image", "text"])
        self.assertTrue(json.loads(stdout.getvalue())["sent"])
        self.assertNotIn("data_base64", stdout.getvalue())
        self.assertTrue(gateway.closed)

    def test_cli_invalid_image_has_clean_error_without_connecting(self):
        with patch.object(cli, "connect") as connect, redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as error:
                main(["send", "describe", "--image", str(self.root / "missing.png")])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("invalid argument:", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        connect.assert_not_called()


class MCPImageSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_parameter_is_exposed_and_forwarded(self):
        from muse_mcp import server
        tools = await server.mcp.list_tools()
        send = next(tool for tool in tools if tool.name == "muse_send")
        self.assertIn("images", send.inputSchema["properties"])
        client = Mock()
        client.send.return_value = {"ok": True, "sent": True}
        with patch.object(server, "get_client", return_value=client):
            await server.mcp.call_tool("muse_send", {"message": "describe", "images": ["/tmp/test.png"], "fire_and_forget": True})
        self.assertEqual(client.send.call_args.kwargs["images"], ["/tmp/test.png"])


if __name__ == "__main__":
    unittest.main()
