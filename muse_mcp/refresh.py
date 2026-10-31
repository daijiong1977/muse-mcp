"""One-command, account-checked browser-to-Linux Muse session refresh."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import uuid


class RefreshError(RuntimeError):
    pass


def run_json(command, json_stderr=False, timeout=90, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, **kwargs)
    if result.returncode:
        raise RefreshError("Connection/export failed. Check SSH, Muse connectivity, and the selected browser's permission.")
    output = result.stderr if json_stderr else result.stdout
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        # ego-browser routes console output to stderr and may add notices.
        candidates = []
        if json_stderr:
            for line in output.splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and "ready" in value:
                    candidates.append(value)
        if len(candidates) != 1:
            raise RefreshError("The helper returned an invalid response") from None
        data = candidates[0]
    if isinstance(data, dict) and data.get("success") is False:
        raise RefreshError("Chrome connection failed. Open the intended Muse profile and allow remote debugging.")
    return data.get("data", data) if isinstance(data, dict) else data


def muse_tab(tabs):
    from urllib.parse import urlparse
    matches = [tab for tab in tabs if isinstance(tab, dict)
               and urlparse(tab.get("url") or "").hostname == "muse.ai"]
    if len(matches) != 1:
        raise RefreshError("Keep exactly one Muse tab in the Chrome instance being exported")
    tab_id = matches[0].get("id") or matches[0].get("tabId")
    if not tab_id:
        raise RefreshError("Chrome did not identify the Muse tab")
    return str(tab_id)


def export_chrome(destination, expected):
    from curl_cffi import requests
    from muse import _hatch_headers

    base = ["agent-browser", "--session", "muse-linux-refresh",
            "--headed", "--auto-connect"]
    data = run_json(base + ["tab", "list", "--json"])
    tab_id = muse_tab(data.get("tabs", []) if isinstance(data, dict) else [])
    run_json(base + ["tab", tab_id, "--json"])
    raw = run_json(base + ["cookies", "get", "--json"])
    cookies = raw if isinstance(raw, list) else raw.get("cookies", raw.get("items", []))
    cookies = [cookie for cookie in cookies if isinstance(cookie, dict)
               and cookie.get("domain", "").lstrip(".") == "muse.ai"]
    if not any(cookie.get("name") == "hatch_sess" for cookie in cookies):
        raise RefreshError("Sign into Muse in Chrome before refreshing")
    header = "; ".join(cookie["name"] + "=" + cookie["value"] for cookie in cookies)
    response = requests.post("https://muse.ai/api/auth/check", headers=_hatch_headers(header),
                             data=b"", impersonate="chrome", timeout=20)
    if response.status_code != 200:
        raise RefreshError("The Chrome Muse session is not valid; sign into Muse again")
    account = str(response.json().get("viewer_id") or "")
    if account != str(expected):
        raise RefreshError(f"Wrong Chrome account: expected {expected}, found {account}. Linux was not changed.")
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain = cookie["domain"]
        fields = [domain, "TRUE" if domain.startswith(".") else "FALSE", cookie.get("path", "/"),
                  "TRUE" if cookie.get("secure") else "FALSE", str(max(0, int(cookie.get("expires", 0) or 0))),
                  cookie["name"], cookie["value"]]
        if any("\n" in field or "\t" in field or "\r" in field for field in fields):
            raise RefreshError("Chrome returned a cookie that cannot be safely exported")
        lines.append("\t".join(fields))
    destination.write_text("\n".join(lines) + "\n")
    os.chmod(destination, 0o600)


def build_ego_script(destination, expected, space_name, resume_space=None, profile_id=None, email=None):
    """Construct a script without accessing browser state."""
    script = r'''const spaces = RESUME === null ? await listTaskSpaces() : [];
const existing = spaces.find(space => space.name === SPACE_NAME);
const task = RESUME !== null ? await takeOverTaskSpace(RESUME) :
  existing ? await takeOverTaskSpace(existing.id) : await taskSpace(SPACE_NAME, OPTIONS);
let page = task.userPage() || task.page('p1');
if (!page.label) page = await task.adopt(page);
await page.goto('https://muse.ai/');
let autoLogin = 'not_needed', step = 'identity';
const check = async () => await page.evaluate(async () => {
  const response = await fetch('/api/auth/check', {method: 'POST'});
  let data = {};
  try { data = await response.json(); } catch {}
  return {status: response.status, accountId: String(data.viewer_id || '')};
});
const chooseRemembered = (labels, email) => {
  const candidates = labels.filter(label => label.startsWith('Log in as '));
  const matches = email ? candidates.filter(label => label === 'Log in as ' + email) : candidates;
  return matches.length === 1 ? matches[0] : null;
};
const examine = async (action, label) => await page.evaluate(({action, label}) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const host = location.hostname.toLowerCase();
  const text = document.body ? document.body.innerText : '';
  const challenge = host === 'muse.ai' ? [...document.querySelectorAll('input')].some(el => visible(el) &&
    (['password', 'tel'].includes((el.type || '').toLowerCase()) ||
     (el.autocomplete || '').toLowerCase().split(/\s+/).includes('one-time-code'))) ||
    /password|verification code|two[ -]factor|captcha|\b2fa\b/i.test(text) :
    [...document.querySelectorAll('input, textarea, [contenteditable]')].some(el => visible(el) &&
      !el.disabled && !el.readOnly && (el.isContentEditable || el.tagName === 'TEXTAREA' ||
        (el.tagName === 'INPUT' && !['hidden', 'button', 'submit', 'checkbox', 'radio'].includes(el.type.toLowerCase())))) ||
    /password|passcode|security code|verification code|confirm.*identity|enter.*code|two[ -]factor|captcha|\b2fa\b|authenticat/i.test(text);
  const buttons = [...document.querySelectorAll('button, [role="button"]')].filter(visible);
  const name = el => el.getAttribute('aria-label') ||
    (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ').trim() ||
    (el.innerText || '').trim();
  const remembered = buttons.map(el => el.getAttribute('aria-label') || '').filter(x => x.startsWith('Log in as '));
  const continues = buttons.map(name).filter(x => x.startsWith('Continue'));
  let clicked = false;
  if (!challenge && action === 'remembered' && host === 'muse.ai') {
    const matches = buttons.filter(el => el.getAttribute('aria-label') === label);
    if (matches.length === 1) { matches[0].click(); clicked = true; }
  }
  if (!challenge && action === 'continue' && /(^|\.)(facebook\.com|meta\.com)$/.test(host)) {
    const matches = buttons.filter(el => name(el).startsWith('Continue'));
    if (matches.length === 1 && name(matches[0]) === label) { matches[0].click(); clicked = true; }
  }
  return {host, challenge, remembered, continues, clicked};
}, {action, label});
let identity = await check();
if (identity.status !== 200) {
  step = 'remembered_account';
  let state = await examine(null, null);
  const selected = chooseRemembered(state.remembered, EMAIL);
  if (!state.challenge && state.host === 'muse.ai' && selected && (await examine('remembered', selected)).clicked) {
    autoLogin = 'clicked';
    step = 'waiting_for_sign_in';
    const deadline = Date.now() + 45000;
    let continued = false;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      try {
        state = await examine(null, null);
        if (state.challenge) { step = 'challenge'; break; }
        if (state.host === 'muse.ai') {
          identity = await check();
          if (identity.status === 200) { step = 'identity'; break; }
        } else if (!continued && /(^|\.)(facebook\.com|meta\.com)$/.test(state.host) && state.continues.length === 1) {
          continued = true; // Never retry an uncertain click.
          if ((await examine('continue', state.continues[0])).clicked) {
            step = 'confirmation';
            await page.goto('https://muse.ai/');
          }
        }
      } catch (error) { /* Navigation may temporarily invalidate evaluation. */ }
    }
  } else if (state.challenge) step = 'challenge';
}
if (identity.status !== 200 || identity.accountId !== EXPECTED) {
  if (identity.status === 200) step = 'wrong_account';
  await task.handOff();
  console.log(JSON.stringify({ready: false, autoLogin: 'handed_off', step, spaceId: task.spaceId}));
} else {
  const response = await page.cdp('Network.getCookies', {urls: ['https://muse.ai/']});
  const cookies = response.cookies.filter(c => c.domain.replace(/^\./, '') === 'muse.ai');
  if (!cookies.some(c => c.name === 'hatch_sess')) throw new Error('Muse login cookie missing');
  const lines = ['# Netscape HTTP Cookie File'];
  for (const c of cookies) {
    const fields = [c.domain, c.domain.startsWith('.') ? 'TRUE' : 'FALSE', c.path || '/',
      c.secure ? 'TRUE' : 'FALSE', String(Math.max(0, Math.trunc(c.expires || 0))), c.name, c.value];
    if (fields.some(value => /[\r\n\t]/.test(value))) throw new Error('Invalid cookie format');
    lines.push(fields.join('\t'));
  }
  const fs = await import('node:fs/promises');
  await fs.writeFile(DESTINATION, lines.join('\n') + '\n', {mode: 0o600, flag: 'wx'});
  await task.finish({keep: []});
  console.log(JSON.stringify({ready: true, accountId: identity.accountId, autoLogin, step}));
}
'''
    replacements = {'RESUME': json.dumps(int(resume_space) if resume_space is not None else None),
                    'SPACE_NAME': json.dumps(space_name), 'OPTIONS': json.dumps({'profileId': profile_id} if profile_id is not None else {}),
                    'EMAIL': json.dumps(email), 'EXPECTED': json.dumps(str(expected)), 'DESTINATION': json.dumps(str(destination))}
    return re.sub(r'\b(RESUME|SPACE_NAME|OPTIONS|EMAIL|EXPECTED|DESTINATION)\b',
                  lambda match: replacements[match.group()], script)


def export_ego(destination, expected, space_name, resume_space=None, profile_id=None, email=None):
    script = build_ego_script(destination, expected, space_name, resume_space, profile_id, email)
    result = run_json(["ego-browser", "nodejs"], json_stderr=True, input=script)
    if not result.get("ready"):
        error = RefreshError("ego(lite) needs the intended Muse sign-in. Sign into its handed-off Space, then retry with --resume-ego " + str(result.get("spaceId")) + ".")
        error.login_result = {"autoLogin": "handed_off", "step": result.get("step", "identity"), "spaceId": result.get("spaceId")}
        raise error
    if str(result.get("accountId")) != str(expected) or not destination.is_file():
        raise RefreshError("ego(lite) did not export the expected Muse session")
    # Independently check the exported cookie before transmitting it to Linux.
    from curl_cffi import requests
    from muse import _hatch_headers, load_cookies
    response = requests.post("https://muse.ai/api/auth/check",
                             headers=_hatch_headers(load_cookies(str(destination))),
                             data=b"", impersonate="chrome", timeout=20)
    if response.status_code != 200 or str(response.json().get("viewer_id")) != str(expected):
        raise RefreshError("Exported ego(lite) session did not pass the account check; Linux was not changed.")

    return {"autoLogin": result.get("autoLogin", "not_needed"), "step": result.get("step", "identity")}


def validate_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name):
        raise RefreshError("Invalid profile name")


def ssh_options(config):
    host = config["host"]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9_.-]*", host):
        raise RefreshError("Expected an SSH destination such as user@host")
    return host, ["-i", str(Path(config["identity_file"]).expanduser()),
                  "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def profile_operation(config, target, operation, name, *extra):
    validate_name(name)
    source = Path(__file__).with_name("refresh_remote.py").read_text()
    if target == "local":
        python = config.get("local_python", str(Path.home() / ".local/share/uv/tools/muse-cli/bin/python"))
        return run_json([python, "-", operation, name, *extra], input=source)
    if target != "linux":
        raise RefreshError("Target must be linux or local")
    host, options = ssh_options(config)
    return run_json(["ssh", *options, host,
                     shlex.join([config["remote_python"], "-", operation, name, *extra])], input=source)


def validate_accounts(accounts):
    if not isinstance(accounts, dict):
        raise RefreshError("accounts must be a profile mapping")
    profile_accounts = {}
    for name, binding in accounts.items():
        validate_name(name)
        if not isinstance(binding, dict) or binding.get("target") not in ("linux", "local"):
            raise RefreshError("Each account needs an explicit linux/local target")
        if "email" in binding and (not isinstance(binding["email"], str) or not binding["email"].strip()):
            raise RefreshError("Account email must be a non-empty string")
        account = binding.get("account_id")
        profile_id = binding.get("ego_profile_id")
        if not isinstance(account, str) or not account.isdigit() or not isinstance(profile_id, str) or not profile_id:
            raise RefreshError("Each account needs its verified account_id and ego_profile_id")
        if profile_id in profile_accounts and profile_accounts[profile_id] != account:
            raise RefreshError("One Ego profile cannot be bound to two different Muse accounts")
        profile_accounts[profile_id] = account


def match_accounts(targets, discovered, previous=None):
    """Match immutable Muse IDs; profile names alone never establish identity."""
    if not targets:
        raise RefreshError("No saved destination profiles found; existing mappings were preserved")
    previous = previous or {}
    matched = {}
    missing = []
    for target in targets:
        name, account = target["name"], str(target["account_id"])
        validate_name(name)
        if name in matched:
            raise RefreshError("The same profile name exists on multiple machines; choose explicit names first")
        candidates = [row for row in discovered if row.get("httpStatus") == 200 and str(row.get("accountId")) == account]
        if not candidates:
            missing.append(name)
            continue
        old_id = previous.get(name, {}).get("ego_profile_id")
        candidates.sort(key=lambda row: (row.get("egoProfileId") != old_id if old_id else not row.get("isDefault", False), row["egoProfileId"]))
        row = candidates[0]
        matched[name] = {"account_id": account, "target": target["target"],
                         "ego_profile_id": row["egoProfileId"], "ego_profile_name": row["egoProfileName"]}
        if previous.get(name, {}).get("account_id") == account and "email" in previous[name]:
            matched[name]["email"] = previous[name]["email"]
    if missing:
        raise RefreshError("No signed-in Ego match for: " + ", ".join(missing) + ". Existing mappings were preserved.")
    validate_accounts(matched)
    return matched


def discover_accounts(config):
    targets = []
    for destination in ("linux", "local"):
        data = profile_operation(config, destination, "list", "inventory")
        targets.extend({**row, "target": destination} for row in data["profiles"])
    script = '''const results = [];
for (const profile of await profiles()) {
  const task = await taskSpace('Muse account matching ' + profile.id, {profileId: profile.id});
  const page = task.page('p1');
  await page.goto('https://muse.ai/');
  const identity = await page.evaluate(async () => {
    const response = await fetch('/api/auth/check', {method: 'POST'});
    let data = {};
    try {data = await response.json();} catch {}
    return {httpStatus: response.status, accountId: data.viewer_id == null ? null : String(data.viewer_id)};
  });
  results.push({egoProfileId: profile.id, egoProfileName: profile.name, isDefault: profile.isDefault, ...identity});
  await task.finish({keep: []});
}
console.log(JSON.stringify({ready: true, profiles: results}));
'''
    data = run_json(["ego-browser", "nodejs"], json_stderr=True, timeout=300, input=script)
    return match_accounts(targets, data["profiles"], config.get("accounts"))


def save_mapping(path, config, accounts):
    # Configuration contains names/IDs/SSH paths, never exported session values.
    if json.loads(path.read_text()) != config:
        raise RefreshError("Configuration changed during matching; rerun instead of overwriting it")
    backup = path.with_name(path.name + ".before-match-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    backup.write_bytes(path.read_bytes())
    os.chmod(backup, 0o600)
    updated = {**config, "browser": "ego", "accounts": accounts}
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(updated, output, indent=2)
            output.write("\n")
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


def refresh_profile(config, name, force=False, browser_override=None, resume_space=None):
    validate_name(name)
    accounts = config.get("accounts", {})
    if accounts and name not in accounts:
        raise RefreshError("Profile is not mapped. Run --match-profiles first.")
    binding = accounts.get(name, {})
    target = binding.get("target", "linux")
    browser = browser_override or config.get("browser", "chrome")
    if browser not in ("chrome", "ego") or (resume_space is not None and browser != "ego"):
        raise RefreshError("Select ego or chrome; --resume-ego requires ego")

    def operation(action, *extra):
        return profile_operation(config, target, action, name, *extra)

    check = operation("check")
    if binding and check.get("account_id") is not None and str(check["account_id"]) != binding["account_id"]:
        raise RefreshError("Saved destination identity differs from its mapping; nothing was exported")
    if check.get("asleep"):
        return {"profile": name, "healthy": None, "asleep": True,
                "note": "Muse machine asleep (502); login not checked; keepalive wakes it"}
    if binding and "account_id" not in check:
        raise RefreshError("Saved destination identity differs from its mapping; nothing was exported")
    if check.get("healthy") and not force:
        return {"profile": name, "target": target, "account_id": check["account_id"],
                "healthy": True, "refreshed": False, "note": "Login works; no browser was needed."}
    pending = "cookies.refresh-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="muse-refresh-") as directory:
        cookie_file = Path(directory) / "cookies.txt"
        if browser == "ego":
            space = config.get("ego_space", "Muse Linux session refresh")
            options = {}
            if binding:
                options["profile_id"] = binding["ego_profile_id"]
                if "email" in binding:
                    options["email"] = binding["email"]
                space += " - " + name + " - " + binding["ego_profile_id"]
            if resume_space is not None:
                options["resume_space"] = resume_space
            login_result = export_ego(cookie_file, check["account_id"], space, **options)
        else:
            export_chrome(cookie_file, check["account_id"])
        if target == "local":
            staged_cookie = Path.home() / ".config/muse-cli" / pending
            with staged_cookie.open("xb") as output:
                output.write(cookie_file.read_bytes())
            os.chmod(staged_cookie, 0o600)
        else:
            host, options = ssh_options(config)
            subprocess.run(["scp", *options, str(cookie_file), host + ":.config/muse-cli/" + pending],
                           capture_output=True, check=True, timeout=45)
        try:
            update_default = target == "linux" and name == config.get("default_login_profile")
            result = operation("apply", pending, "1" if update_default else "0")
        finally:
            try:
                operation("cleanup", pending)
            except (RefreshError, OSError, subprocess.SubprocessError):
                pass
        result["target"] = target
        if browser == "ego" and login_result:
            result.update(login_result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?", help="One saved profile (defaults to configuration)")
    parser.add_argument("--force", action="store_true", help="Refresh even if the saved login works")
    parser.add_argument("--all", action="store_true", help="Check/refresh all mapped destinations")
    parser.add_argument("--list", action="store_true", help="Show account mappings without reading cookies")
    parser.add_argument("--match-profiles", action="store_true", help="Match signed-in Ego profiles to saved local/Linux account IDs")
    parser.add_argument("--browser", choices=("chrome", "ego"))
    parser.add_argument("--resume-ego", type=int)
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/muse-refresh-linux/config.json")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if sum((args.all, args.list, args.match_profiles)) > 1 or (args.profile and (args.all or args.list or args.match_profiles)):
            raise RefreshError("Choose one profile, --all, --list, or --match-profiles")
        if args.all and args.resume_ego is not None:
            raise RefreshError("Resume one named account, not --all")
        config = json.loads(args.config.read_text())
        if args.match_profiles:
            accounts = discover_accounts(config)
            save_mapping(args.config, config, accounts)
            print(json.dumps({"matched": accounts, "saved": True}), flush=True)
            return 0
        accounts = config.get("accounts", {})
        validate_accounts(accounts)
        if args.list:
            print(json.dumps(accounts, indent=2), flush=True)
            return 0
        names = list(accounts) if args.all else [args.profile or config["profile"]]
        if not names:
            raise RefreshError("No accounts mapped. Run --match-profiles first.")
        failed = False
        for name in names:
            try:
                result = refresh_profile(config, name, args.force, args.browser, args.resume_ego)
            except Exception as error:
                if not args.all:
                    raise
                failed = True
                result = {"profile": name, "ok": False, "error": str(error) if isinstance(error, RefreshError) else "Refresh failed; check this account's browser sign-in and connectivity."}
                result.update(getattr(error, "login_result", {}))
            print(json.dumps(result), flush=True)
        return int(failed)
    except Exception as error:
        import sys
        message = str(error) if isinstance(error, RefreshError) else "Refresh failed; check configuration, SSH, and browser sign-in."
        if hasattr(error, "login_result"):
            print(json.dumps({"ok": False, "error": message, **error.login_result}), flush=True)
        else:
            print(message, file=sys.stderr)
        return 1
