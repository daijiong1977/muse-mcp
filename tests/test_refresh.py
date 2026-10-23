import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from muse_mcp.refresh import RefreshError, export_chrome, export_ego, main, muse_tab, run_json


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "profile": "primary-demo", "host": "user@example.org",
            "identity_file": str(self.root / "key"),
            "remote_python": "/home/user/.local/share/uv/tools/muse-cli/bin/python",
            "default_login_profile": "primary-demo",
        }))

    def test_healthy_server_does_not_use_chrome_or_copy(self):
        with patch("muse_mcp.refresh.run_json", return_value={"healthy": True, "account_id": "123"}) as remote, \
                patch("muse_mcp.refresh.export_chrome") as export, \
                patch("muse_mcp.refresh.subprocess.run") as copy, redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["--config", str(self.config)]), 0)
        self.assertFalse(json.loads(stdout.getvalue())["refreshed"])
        self.assertEqual(remote.call_count, 1)
        export.assert_not_called()
        copy.assert_not_called()

    def test_force_refresh_checks_identity_before_copy_and_cleans_local_file(self):
        observed = []
        def export(path, expected):
            self.assertEqual(expected, "123")
            path.write_text("hatch_sess=private")
            observed.append(path)
        replies = [{"healthy": True, "account_id": "123"}, {"refreshed": True}, {"staging_removed": True}]
        with patch("muse_mcp.refresh.run_json", side_effect=replies) as remote, \
                patch("muse_mcp.refresh.export_chrome", side_effect=export), \
                patch("muse_mcp.refresh.subprocess.run") as copy, redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["--config", str(self.config), "--force"]), 0)
        self.assertTrue(json.loads(stdout.getvalue())["refreshed"])
        self.assertFalse(observed[0].exists())
        self.assertEqual(copy.call_args.args[0][0], "scp")
        self.assertNotIn("private", str(copy.call_args))
        self.assertIn("cleanup", remote.call_args_list[-1].args[0][-1])

    def test_wrong_browser_account_cannot_upload(self):
        with patch("muse_mcp.refresh.run_json", return_value={"healthy": False, "account_id": "123"}), \
                patch("muse_mcp.refresh.export_chrome", side_effect=RefreshError("Wrong Chrome account")), \
                patch("muse_mcp.refresh.subprocess.run") as copy, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.config)]), 1)
        copy.assert_not_called()

    def test_bad_profile_is_rejected_before_ssh(self):
        with patch("muse_mcp.refresh.run_json") as remote, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["../other", "--config", str(self.config)]), 1)
        remote.assert_not_called()

    def test_tab_selection_requires_one_actual_muse_origin(self):
        self.assertEqual(muse_tab([{"id": "one", "url": "https://muse.ai/thread/123"}]), "one")
        for tabs in ([], [{"id": "bad", "url": "https://muse.ai.example.org"}],
                     [{"id": "one", "url": "https://muse.ai"}, {"id": "two", "url": "https://muse.ai"}]):
            with self.assertRaises(RefreshError):
                muse_tab(tabs)

    def test_export_restricts_domains_and_validates_account(self):
        target = self.root / "cookies.txt"
        responses = [{"tabs": [{"id": "one", "url": "https://muse.ai/"}]}, {},
                     {"cookies": [{"name": "hatch_sess", "value": "private-cookie", "domain": ".muse.ai", "expires": -1},
                                  {"name": "other", "value": "unrelated", "domain": "example.org"}]}]
        response = SimpleNamespace(status_code=200, json=lambda: {"viewer_id": "123"})
        with patch("muse_mcp.refresh.run_json", side_effect=responses), \
                patch("curl_cffi.requests.post", return_value=response) as post:
            export_chrome(target, "123")
        self.assertIn("hatch_sess", target.read_text())
        self.assertNotIn("unrelated", target.read_text())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("unrelated", post.call_args.kwargs["headers"]["Cookie"])
        target.unlink()
        with patch("muse_mcp.refresh.run_json", side_effect=responses), \
                patch("curl_cffi.requests.post", return_value=response):
            with self.assertRaisesRegex(RefreshError, "Wrong Chrome account"):
                export_chrome(target, "999")
        self.assertFalse(target.exists())

    def test_ego_console_json_uses_stderr(self):
        response = SimpleNamespace(returncode=0, stdout="", stderr='notice\n{"ready":true,"accountId":"123"}\n')
        with patch("muse_mcp.refresh.subprocess.run", return_value=response):
            self.assertEqual(run_json(["ego-browser", "nodejs"], json_stderr=True)["accountId"], "123")

    def test_ego_export_verifies_private_file_without_printing_cookies(self):
        target = self.root / "cookies.txt"
        def browser(command, **kwargs):
            self.assertTrue(kwargs["json_stderr"])
            self.assertNotIn("private-cookie", kwargs["input"])
            target.write_text("hatch_sess=private-cookie")
            return {"ready": True, "accountId": "123"}
        response = SimpleNamespace(status_code=200, json=lambda: {"viewer_id": "123"})
        with patch("muse_mcp.refresh.run_json", side_effect=browser), \
                patch("curl_cffi.requests.post", return_value=response):
            export_ego(target, "123", "test-space")
        self.assertTrue(target.is_file())

    def test_ego_wrong_account_hands_off_without_upload(self):
        with patch("muse_mcp.refresh.run_json", return_value={"ready": False, "accountId": "999"}), \
                patch("curl_cffi.requests.post") as http:
            with self.assertRaisesRegex(RefreshError, "sign-in"):
                export_ego(self.root / "cookies.txt", "123", "test-space")
        http.assert_not_called()

    def test_browser_option_selects_ego(self):
        def browser(path, expected, space):
            self.assertEqual(expected, "123")
            path.write_text("hatch_sess=private")
        with patch("muse_mcp.refresh.run_json", side_effect=[{"healthy": False, "account_id": "123"}, {"refreshed": True}, {"staging_removed": True}]), \
                patch("muse_mcp.refresh.export_ego", side_effect=browser) as ego, \
                patch("muse_mcp.refresh.export_chrome") as chrome, \
                patch("muse_mcp.refresh.subprocess.run"), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.config), "--browser", "ego"]), 0)
        self.assertEqual(ego.call_count, 1)
        chrome.assert_not_called()


if __name__ == "__main__":
    unittest.main()
