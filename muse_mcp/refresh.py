"""One-command, account-checked browser-to-Linux Muse session refresh."""

import argparse
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


def run_json(command, json_stderr=False, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=90, **kwargs)
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


def export_ego(destination, expected, space_name, resume_space=None):
    # Only the expected ID and private output path enter the script, never cookies.
    script = '''const task = await TASK;
let page = RESUMING ? task.userPage() : task.page('p1');
if (!page.label) page = await task.adopt(page);
await page.goto('https://muse.ai/');
const identity = await page.evaluate(async () => {
  const response = await fetch('/api/auth/check', {method: 'POST'});
  let data = {};
  try { data = await response.json(); } catch {}
  return {status: response.status, accountId: String(data.viewer_id || '')};
});
if (identity.status !== 200 || identity.accountId !== EXPECTED) {
  await task.handOff();
  console.log(JSON.stringify({ready: false, accountId: identity.accountId,
    spaceId: task.spaceId, note: 'Sign into the intended Muse account in this Space, then retry.'}));
} else {
  const response = await page.cdp('Network.getCookies', {urls: ['https://muse.ai/']});
  const cookies = response.cookies.filter(c => c.domain.replace(/^\\./, '') === 'muse.ai');
  if (!cookies.some(c => c.name === 'hatch_sess')) throw new Error('Muse login cookie missing');
  const lines = ['# Netscape HTTP Cookie File'];
  for (const c of cookies) {
    const fields = [c.domain, c.domain.startsWith('.') ? 'TRUE' : 'FALSE', c.path || '/',
      c.secure ? 'TRUE' : 'FALSE', String(Math.max(0, Math.trunc(c.expires || 0))), c.name, c.value];
    if (fields.some(value => /[\\r\\n\\t]/.test(value))) throw new Error('Invalid cookie format');
    lines.push(fields.join('\\t'));
  }
  const fs = await import('node:fs/promises');
  await fs.writeFile(DESTINATION, lines.join('\\n') + '\\n', {mode: 0o600, flag: 'wx'});
  await task.finish({keep: []});
  console.log(JSON.stringify({ready: true, accountId: identity.accountId}));
}
'''.replace('TASK', 'takeOverTaskSpace(' + str(int(resume_space)) + ')' if resume_space is not None else 'taskSpace(' + json.dumps(space_name) + ')').replace('RESUMING', 'true' if resume_space is not None else 'false').replace('EXPECTED', json.dumps(str(expected))).replace('DESTINATION', json.dumps(str(destination)))
    result = run_json(["ego-browser", "nodejs"], json_stderr=True, input=script)
    if not result.get("ready"):
        raise RefreshError("ego(lite) needs the intended Muse sign-in. Sign into its handed-off Space, then retry with --resume-ego " + str(result.get("spaceId")) + ".")
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?", help="Saved Linux profile (defaults to configuration)")
    parser.add_argument("--force", action="store_true", help="Refresh even if the Linux login is healthy")
    parser.add_argument("--browser", choices=("chrome", "ego"), help="Browser source (defaults to configuration, then Chrome)")
    parser.add_argument("--resume-ego", type=int, help="Resume the handed-off ego Space after completing sign-in")
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/muse-refresh-linux/config.json")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        config = json.loads(args.config.read_text())
        name = args.profile or config["profile"]
        browser = args.browser or config.get("browser", "chrome")
        if browser not in ("chrome", "ego"):
            raise RefreshError("Browser must be chrome or ego")
        if args.resume_ego is not None and browser != "ego":
            raise RefreshError("--resume-ego requires the ego browser source")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name):
            raise RefreshError("Invalid profile name")
        host = config["host"]
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9_.-]*", host):
            raise RefreshError("Expected an SSH destination such as user@host")
        identity = str(Path(config["identity_file"]).expanduser())
        options = ["-i", identity, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        source = Path(__file__).with_name("refresh_remote.py").read_text()
        remote_python = config["remote_python"]

        def remote(operation, *extra):
            return run_json(["ssh", *options, host,
                             shlex.join([remote_python, "-", operation, name, *extra])], input=source)

        check = remote("check")
        if check.get("healthy") and not args.force:
            print(json.dumps({"profile": name, "account_id": check["account_id"],
                              "healthy": True, "refreshed": False, "note": "Login works; no browser was needed."}))
            return 0
        pending = "cookies.refresh-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="muse-refresh-") as directory:
            cookie_file = Path(directory) / "cookies.txt"
            if browser == "ego":
                ego_args = (cookie_file, check["account_id"], config.get("ego_space", "Muse Linux session refresh"))
                if args.resume_ego is None:
                    export_ego(*ego_args)
                else:
                    export_ego(*ego_args, resume_space=args.resume_ego)
            else:
                export_chrome(cookie_file, check["account_id"])
            subprocess.run(["scp", *options, str(cookie_file),
                            host + ":.config/muse-cli/" + pending],
                           capture_output=True, check=True, timeout=45)
            try:
                result = remote("apply", pending, "1" if name == config.get("default_login_profile") else "0")
            finally:
                try:
                    remote("cleanup", pending)
                except (RefreshError, OSError, subprocess.SubprocessError):
                    pass  # It stays private on Linux if the SSH connection drops.
            print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, RefreshError, subprocess.SubprocessError) as error:
        # Controlled messages only: never print captured cookie output or HTTP bodies.
        import sys
        message = str(error) if isinstance(error, RefreshError) else "Refresh failed; check configuration, SSH, and Chrome sign-in."
        print(message, file=sys.stderr)
        return 1
