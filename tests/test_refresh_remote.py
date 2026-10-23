import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from muse_mcp.refresh_remote import main


class RemoteRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / ".config/muse-cli"
        self.profile = self.root / "profiles/primary-demo"
        self.profile.mkdir(parents=True)
        (self.profile / "metadata.json").write_text(json.dumps({"account_id": "123"}))
        (self.profile / "cookies.txt").write_text("old")
        (self.root / "cookies.txt").write_text("old")
        (self.root / "config.json").write_text(json.dumps({"cookies_file": str(self.root / "cookies.txt"), "node_id": "keep", "vm_id": "stale"}))
        self.pending_name = "cookies.refresh-" + "a" * 32
        self.pending = self.root / self.pending_name
        self.pending.write_text("new")
        gateway = ModuleType("muse_cli.gateway")
        gateway.Gateway = Mock()
        gateway._hatch_headers = lambda cookie: {}
        gateway.load_cookies = lambda filename: Path(filename).read_text()
        self.fake_gateway = gateway
        self.patches = [patch("pathlib.Path.home", return_value=self.home),
                        patch.dict(sys.modules, {"muse_cli": ModuleType("muse_cli"), "muse_cli.gateway": gateway})]
        for replacement in self.patches:
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_success_backs_up_defaults_and_preserves_config(self):
        def subprocess_run(command, **kwargs):
            if command[1] == "import":
                self.assertEqual(command[-2:], ["--expect-account", "123"])
                (self.profile / "cookies.txt").write_text(self.pending.read_text())
            return SimpleNamespace(returncode=0)
        with patch.object(sys, "argv", ["refresh", "apply", "primary-demo", self.pending_name, "1"]), \
                patch("muse_mcp.refresh_remote.subprocess.run", side_effect=subprocess_run), redirect_stdout(io.StringIO()) as stdout:
            main()
        self.assertTrue(json.loads(stdout.getvalue())["default_refreshed"])
        self.assertEqual((self.root / "cookies.txt").read_text(), "new")
        config = json.loads((self.root / "config.json").read_text())
        self.assertEqual(config["node_id"], "keep")
        self.assertNotIn("vm_id", config)
        backup = next((self.root / "backups").iterdir())
        self.assertEqual((backup / "cookies.txt").read_text(), "old")
        self.assertEqual((self.root / "cookies.txt").stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.pending.exists())

    def test_import_rejection_keeps_defaults_and_cleans_staging(self):
        with patch.object(sys, "argv", ["refresh", "apply", "primary-demo", self.pending_name, "1"]), \
                patch("muse_mcp.refresh_remote.subprocess.run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(RuntimeError):
                main()
        self.assertEqual((self.root / "cookies.txt").read_text(), "old")
        self.assertEqual((self.profile / "cookies.txt").read_text(), "old")
        self.assertFalse(self.pending.exists())

    def test_different_default_account_is_never_overwritten(self):
        (self.root / "cookies.txt").write_text("another account")
        with patch.object(sys, "argv", ["refresh", "apply", "primary-demo", self.pending_name, "1"]), \
                patch("muse_mcp.refresh_remote.subprocess.run") as imported:
            with self.assertRaisesRegex(ValueError, "different saved sessions"):
                main()
        imported.assert_not_called()
        self.assertEqual((self.root / "cookies.txt").read_text(), "another account")
        self.assertFalse(self.pending.exists())

    def test_401_requests_refresh_without_exposing_body(self):
        with patch.object(sys, "argv", ["refresh", "check", "primary-demo"]), \
                patch("curl_cffi.requests.post", return_value=SimpleNamespace(status_code=401)), redirect_stdout(io.StringIO()) as stdout:
            main()
        self.assertEqual(json.loads(stdout.getvalue()), {"healthy": False, "account_id": "123"})
        self.fake_gateway.Gateway.assert_not_called()

    def test_inventory_lists_identities_without_reading_cookies(self):
        (self.root / "profiles/alias").symlink_to(self.profile)
        with patch.object(sys, "argv", ["refresh", "list", "inventory"]), redirect_stdout(io.StringIO()) as stdout:
            main()
        self.assertEqual(json.loads(stdout.getvalue()), {"profiles": [{"name": "primary-demo", "account_id": "123"}]})
        self.fake_gateway.Gateway.assert_not_called()


if __name__ == "__main__":
    unittest.main()
