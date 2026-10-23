import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from muse_mcp.refresh import RefreshError, export_ego, main, match_accounts, refresh_profile, validate_accounts


class MultiRefreshTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.config_path = self.home / "config.json"
        self.accounts = {
            "primary-demo": {"account_id": "123", "ego_profile_id": "Profile 1", "target": "linux"},
            "fourth": {"account_id": "456", "ego_profile_id": "Profile 4", "target": "local"},
        }
        self.config = {"profile": "primary-demo", "browser": "ego", "accounts": self.accounts}
        self.config_path.write_text(json.dumps(self.config))

    def test_identity_matching_ignores_names_and_prefers_default_duplicate(self):
        targets = [{"name": name, "account_id": binding["account_id"], "target": binding["target"]}
                   for name, binding in self.accounts.items()]
        discovered = [
            {"egoProfileId": "Profile 2", "egoProfileName": "Primary", "accountId": "123", "httpStatus": 200},
            {"egoProfileId": "Profile 1", "egoProfileName": "Anything", "accountId": "123", "httpStatus": 200, "isDefault": True},
            {"egoProfileId": "Profile 4", "egoProfileName": "Primary", "accountId": "456", "httpStatus": 200},
            {"egoProfileId": "Profile 5", "egoProfileName": "Fourth", "accountId": None, "httpStatus": 401},
        ]
        mapping = match_accounts(targets, discovered)
        self.assertEqual(mapping["primary-demo"]["ego_profile_id"], "Profile 1")
        self.assertEqual(mapping["fourth"]["ego_profile_id"], "Profile 4")
        self.assertEqual(mapping["fourth"]["target"], "local")
        previous = {"primary-demo": {"ego_profile_id": "Profile 2"}}
        self.assertEqual(match_accounts(targets, discovered, previous)["primary-demo"]["ego_profile_id"], "Profile 2")

    def test_missing_match_never_replaces_existing_mapping(self):
        original = self.config_path.read_bytes()
        with patch("muse_mcp.refresh.discover_accounts", side_effect=RefreshError("No signed-in Ego match")), \
                patch("muse_mcp.refresh.save_mapping") as save, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.config_path), "--match-profiles"]), 1)
        save.assert_not_called()
        self.assertEqual(self.config_path.read_bytes(), original)

    def test_inconsistent_browser_binding_is_rejected(self):
        self.accounts["fourth"]["ego_profile_id"] = "Profile 1"
        with self.assertRaisesRegex(RefreshError, "two different Muse accounts"):
            validate_accounts(self.accounts)

    def test_destination_identity_mismatch_stops_before_export(self):
        with patch("muse_mcp.refresh.profile_operation", return_value={"healthy": False, "account_id": "999"}), \
                patch("muse_mcp.refresh.export_ego") as export:
            with self.assertRaisesRegex(RefreshError, "identity differs"):
                refresh_profile(self.config, "primary-demo", force=True)
        export.assert_not_called()

    def test_local_account_never_uses_scp_or_changes_default(self):
        (self.home / ".config/muse-cli").mkdir(parents=True)
        observed = []
        def operation(config, target, action, name, *extra):
            observed.append((target, action, name, extra))
            if action == "check":
                return {"healthy": False, "account_id": "456"}
            staged = self.home / ".config/muse-cli" / extra[0]
            if action == "apply":
                self.assertEqual(extra[1], "0")
                self.assertEqual(staged.read_text(), "private-cookie")
                self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
                return {"refreshed": True}
            staged.unlink()
            return {"staging_removed": True}
        def export(path, expected, space, **options):
            self.assertEqual(expected, "456")
            self.assertEqual(options["profile_id"], "Profile 4")
            path.write_text("private-cookie")
        with patch("muse_mcp.refresh.Path.home", return_value=self.home), \
                patch("muse_mcp.refresh.profile_operation", side_effect=operation), \
                patch("muse_mcp.refresh.export_ego", side_effect=export), \
                patch("muse_mcp.refresh.subprocess.run") as subprocess:
            result = refresh_profile(self.config, "fourth", force=True)
        self.assertEqual(result["target"], "local")
        self.assertTrue(all(target == "local" for target, *_ in observed))
        subprocess.assert_not_called()
        self.assertEqual(list((self.home / ".config/muse-cli").iterdir()), [])

    def test_all_reports_partial_failure_and_continues(self):
        self.config["accounts"]["third"] = {"account_id": "789", "target": "linux", "ego_profile_id": "Profile 3"}
        self.config_path.write_text(json.dumps(self.config))
        outcomes = [{"profile": "primary-demo", "refreshed": True}, RefreshError("Needs sign-in"),
                    {"profile": "third", "healthy": True}]
        with patch("muse_mcp.refresh.refresh_profile", side_effect=outcomes) as refresh, redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["--config", str(self.config_path), "--all"]), 1)
        self.assertEqual(refresh.call_count, 3)
        rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(rows[1]["profile"], "fourth")
        self.assertFalse(rows[1]["ok"])
        self.assertEqual(rows[2]["profile"], "third")

    def test_list_is_read_only_and_needs_no_browser_or_ssh(self):
        with patch("muse_mcp.refresh.run_json") as calls, redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["--config", str(self.config_path), "--list"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["fourth"]["target"], "local")
        calls.assert_not_called()

    def test_profile_selection_is_json_escaped_and_template_values_not_replaced_twice(self):
        path = self.home / "cookies.txt"
        def browser(command, **options):
            self.assertIn('"profileId": "Profile 4"', options["input"])
            self.assertIn('"EXPECTED DESTINATION"', options["input"])
            path.write_text("hatch_sess=private")
            return {"ready": True, "accountId": "456"}
        response = SimpleNamespace(status_code=200, json=lambda: {"viewer_id": "456"})
        with patch("muse_mcp.refresh.run_json", side_effect=browser), patch("curl_cffi.requests.post", return_value=response):
            export_ego(path, "456", "EXPECTED DESTINATION", profile_id="Profile 4")


if __name__ == "__main__":
    unittest.main()
