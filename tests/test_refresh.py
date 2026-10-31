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

class EgoScriptTests(unittest.TestCase):
    def test_builder_guards_and_escaping(self):
        from muse_mcp.refresh import build_ego_script
        script = build_ego_script('/tmp/export', '123', 'a"space', 7, 'profile', 'a@example.org')
        for guard in ('await listTaskSpaces()', 'space.name === "a\\"space"',
                      'takeOverTaskSpace(existing.id)', "task.userPage() || task.page('p1')",
                      'matches.length === 1', "identity.accountId !== \"123\"",
                      'one-time-code', 'captcha', '45000', '"a@example.org"'):
            self.assertIn(guard, script)
        self.assertIn("fields.join('\\t')", script)
        self.assertIn("lines.join('\\n')", script)
        self.assertNotIn('.fill(', script)
        self.assertNotIn('.type(', script)

    def test_email_mapping_preserved_and_validated(self):
        from muse_mcp.refresh import match_accounts, validate_accounts
        old = {'demo': {'account_id': '123', 'target': 'local', 'ego_profile_id': 'p', 'email': 'a@example.org'}}
        targets = [{'name': 'demo', 'account_id': '123', 'target': 'local'}]
        found = [{'httpStatus': 200, 'accountId': '123', 'egoProfileId': 'p', 'egoProfileName': 'P'}]
        self.assertEqual(match_accounts(targets, found, old)['demo']['email'], 'a@example.org')
        old['demo']['email'] = 42
        with self.assertRaises(RefreshError):
            validate_accounts(old)

    def run_fake_ego(self, scenario):
        import shutil
        import subprocess
        from muse_mcp.refresh import build_ego_script
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is unavailable')
        harness = r'''const scenario = SCENARIO;
let clicks = [], created = 0, reused = 0, p1 = 0, handed = 0;
const visible = {offsetWidth: 1, offsetHeight: 1, getClientRects: () => [1]};
const makeButtons = labels => labels.map(label => ({...visible, innerText: label,
  getAttribute: key => key === 'aria-label' ? label : null,
  click: () => {
    clicks.push(label);
    if (scenario.provider) location.hostname = scenario.provider;
  }}));
const buttons = makeButtons(scenario.buttons || []);
const continues = makeButtons(['Continue as Example']);
globalThis.location = {hostname: 'muse.ai'};
const fields = (scenario.fields || []).map(field => ({...visible, tagName: 'INPUT', type: 'text', ...field,
  getClientRects: () => field.type === 'hidden' ? [] : [1]}));
globalThis.document = {get body() {return {innerText: clicks.length ? scenario.text || '' : ''}},
  getElementById: () => null,
  querySelectorAll: selector => {
    if (selector === 'input' || selector === 'input, textarea, [contenteditable]') {
      if (!clicks.length) return [];
      return scenario.challenge ? [{...visible, tagName: 'INPUT', type: 'password'}] : fields;
    }
    return scenario.provider && clicks.length ? continues : buttons;
  }};
globalThis.fetch = async () => ({status: scenario.initial || (clicks.length >= (scenario.provider ? 2 : 1) ? 200 : 401),
  json: async () => ({viewer_id: scenario.wrong && clicks.length ? '999' : '123'})});
globalThis.setTimeout = fn => fn();
const fakePage = {label: 'p1', goto: async () => {location.hostname = 'muse.ai'}, evaluate: async (fn, arg) => fn(arg)};
const fakeTask = {spaceId: 7, userPage: () => undefined, page: () => {p1++; return fakePage},
  handOff: async () => {handed++}, finish: async () => {}};
const listTaskSpaces = async () => scenario.existing ? [{name: 'space', id: 7}] : [];
const takeOverTaskSpace = async () => {reused++; return fakeTask};
const taskSpace = async () => {created++; return fakeTask};
'''
        script = build_ego_script('/tmp/unused', '123', 'space', 7 if scenario.get('resume') else None,
                                  'profile', scenario.get('email'))
        # Exercise all control flow but replace the private file export with a fake.
        script = script[:script.index("  const response = await page.cdp")] + "  console.log(JSON.stringify({ready: true, autoLogin, step}));\n}\n"
        script += "console.log(JSON.stringify({clicks, created, reused, p1, handed}));\n"
        source = harness.replace('SCENARIO', json.dumps(scenario)) + script
        result = subprocess.run([node, '--input-type=module'], input=source, text=True, capture_output=True, timeout=5, check=True)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_flow_with_fake_ego(self):
        scenarios = [
            ({'initial': 200}, 'not_needed', 0, True),
            ({'buttons': ['Log in as a@example.org']}, 'clicked', 1, True),
            ({'buttons': ['Log in as a@example.org', 'Log in as b@example.org']}, 'handed_off', 0, False),
            ({'buttons': ['Log in as a@example.org', 'Log in as b@example.org'], 'email': 'b@example.org'}, 'clicked', 1, True),
            ({'buttons': ['Log in as a@example.org'], 'challenge': True}, 'handed_off', 1, False),
            ({'buttons': ['Log in as a@example.org'], 'wrong': True}, 'handed_off', 1, False),
            ({'initial': 200, 'existing': True}, 'not_needed', 0, True),
            ({'initial': 200, 'resume': True}, 'not_needed', 0, True),
        ]
        for scenario, expected, count, ready in scenarios:
            with self.subTest(scenario=scenario):
                outcome, stats = self.run_fake_ego(scenario)
                self.assertEqual((outcome['autoLogin'], outcome['ready']), (expected, ready))
                self.assertEqual(len(stats['clicks']), count)
                self.assertEqual(stats['handed'], int(not ready))
                self.assertEqual(stats['p1'], 1)
                if scenario.get('email'):
                    self.assertEqual(stats['clicks'], ['Log in as b@example.org'])
                if scenario.get('existing') or scenario.get('resume'):
                    self.assertEqual((stats['created'], stats['reused']), (0, 1))

    def test_security_code_text_input_hands_off_without_continue(self):
        outcome, stats = self.run_fake_ego({
            'buttons': ['Log in as a@example.org'], 'provider': 'www.facebook.com',
            'text': 'Enter your security code', 'fields': [{'type': 'text'}],
        })
        self.assertEqual((outcome['autoLogin'], outcome['step']), ('handed_off', 'challenge'))
        self.assertFalse(outcome['ready'])
        self.assertEqual(stats['handed'], 1)
        self.assertEqual(stats['clicks'], ['Log in as a@example.org'])

    def test_meta_continue_without_editable_fields_is_clicked(self):
        outcome, stats = self.run_fake_ego({
            'buttons': ['Log in as a@example.org'], 'provider': 'auth.meta.com',
            'text': 'Continue as Example',
        })
        self.assertTrue(outcome['ready'])
        self.assertEqual(stats['handed'], 0)
        self.assertEqual(stats['clicks'], ['Log in as a@example.org', 'Continue as Example'])

    def test_hidden_input_only_is_not_a_challenge(self):
        outcome, stats = self.run_fake_ego({
            'buttons': ['Log in as a@example.org'], 'provider': 'www.facebook.com',
            'fields': [{'type': 'hidden', 'offsetWidth': 0, 'offsetHeight': 0}],
        })
        self.assertTrue(outcome['ready'])
        self.assertEqual(stats['handed'], 0)
        self.assertEqual(stats['clicks'], ['Log in as a@example.org', 'Continue as Example'])

    def test_external_editable_fields_and_challenge_text_each_hand_off(self):
        cases = [{'fields': [field]} for field in (
            {'type': 'text'}, {'type': 'email'}, {'type': 'number'},
            {'tagName': 'TEXTAREA'}, {'tagName': 'DIV', 'isContentEditable': True},
        )] + [{'text': text} for text in (
            'Password', 'Passcode', 'Security code', 'Verification code',
            'Confirm your identity', 'Enter your code', 'Two-factor',
            'Two factor', 'CAPTCHA', '2FA', 'Authentication',
        )]
        for case in cases:
            with self.subTest(case=case):
                outcome, stats = self.run_fake_ego({
                    'buttons': ['Log in as a@example.org'], 'provider': 'www.facebook.com', **case,
                })
                self.assertEqual((outcome['autoLogin'], outcome['step']), ('handed_off', 'challenge'))
                self.assertEqual(stats['clicks'], ['Log in as a@example.org'])

    def test_muse_keeps_existing_challenge_rule(self):
        outcome, stats = self.run_fake_ego({
            'buttons': ['Log in as a@example.org'],
            'fields': [{'type': 'text'}], 'text': 'Enter your security code',
        })
        self.assertTrue(outcome['ready'])
        self.assertEqual(stats['handed'], 0)
