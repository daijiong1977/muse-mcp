"""SSH-side refresh helper, sent as source over stdin; never prints credentials."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def main():
    from curl_cffi import requests
    from muse_cli.gateway import Gateway, _hatch_headers, load_cookies

    action, name, *args = sys.argv[1:]
    root = Path.home() / ".config/muse-cli"
    if action == "list":
        profiles = []
        for directory in sorted((root / "profiles").iterdir()):
            if directory.is_dir() and not directory.is_symlink() and (directory / "metadata.json").is_file():
                metadata = json.loads((directory / "metadata.json").read_text())
                profiles.append({"name": directory.name, "account_id": str(metadata["account_id"])})
        print(json.dumps({"profiles": profiles}))
        return
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name):
        raise ValueError("Invalid profile name")
    if action == "cleanup" and len(args) == 1:
        if not re.fullmatch(r"cookies\.refresh-[a-f0-9]{32}", args[0]):
            raise ValueError("Invalid staging filename")
        (root / args[0]).unlink(missing_ok=True)
        print(json.dumps({"staging_removed": True}))
        return
    directory = root / "profiles" / name
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Expected an existing real profile directory")
    metadata = json.loads((directory / "metadata.json").read_text())
    expected = str(metadata["account_id"])
    cookie = directory / "cookies.txt"
    if action == "check":
        try:
            response = requests.post(
                "https://muse.ai/api/auth/check", headers=_hatch_headers(load_cookies(str(cookie))),
                data=b"", impersonate="chrome", timeout=20,
            )
            if response.status_code in (502, 503, 504):
                print(json.dumps({"asleep": True, "account_id": expected}))
                return
            if response.status_code == 401:
                print(json.dumps({"healthy": False, "account_id": expected}))
                return
            if response.status_code != 200:
                raise RuntimeError(f"Authentication check returned HTTP {response.status_code}; not refreshing")
            if str(response.json().get("viewer_id")) != expected:
                raise ValueError("Saved session does not match the profile identity")
            gateway = Gateway(load_cookies(str(cookie)))
            gateway.close()
        except requests.errors.CurlError as error:
            if any(marker in str(error) for marker in ("502", "503", "504", "WebSocket upgrade")):
                print(json.dumps({"asleep": True, "account_id": expected}))
                return
            raise
        print(json.dumps({"healthy": True, "account_id": expected}))
        return
    if action != "apply" or len(args) != 2:
        raise ValueError("Invalid refresh operation")
    pending_name, refresh_default = args
    if not re.fullmatch(r"cookies\.refresh-[a-f0-9]{32}", pending_name):
        raise ValueError("Invalid staging filename")
    pending = root / pending_name
    if pending.is_symlink():
        raise ValueError("Staging file must not be a symbolic link")
    try:
        os.chmod(pending, 0o600)
        default_config = root / "config.json"
        config = json.loads(default_config.read_text()) if refresh_default == "1" else None
        if config is not None:
            default_cookie = Path(config.get("cookies_file", str(root / "cookies.txt")))
            if default_cookie != root / "cookies.txt":
                raise ValueError("Default login uses a nonstandard cookie path; not replacing it")
            if default_cookie.read_bytes() != cookie.read_bytes():
                raise ValueError("Default and selected profile have different saved sessions; not replacing them")
            backup = root / "backups" / ("before-command-refresh-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
            backup.mkdir(mode=0o700, parents=True)
            os.chmod(backup.parent, 0o700)
            for filename in ("cookies.txt", "config.json"):
                shutil.copy2(root / filename, backup / filename)
                os.chmod(backup / filename, 0o600)
        # The existing importer checks the new account ID and gateway before writing.
        helper = str(Path.home() / ".local/bin/muse-profile")
        imported = subprocess.run(
            [helper, "import", name, str(pending), "--expect-account", expected],
            capture_output=True, text=True, timeout=60,
        )
        if imported.returncode:
            raise RuntimeError("Server import failed; old profile backups are retained")
        if config is not None:
            config["cookies_file"] = str(root / "cookies.txt")
            config.pop("vm_id", None)
            for target, data in [(root / "cookies.txt", pending.read_bytes()),
                                 (default_config, (json.dumps(config, indent=2) + "\n").encode())]:
                fd, staged = tempfile.mkstemp(dir=root, prefix=".refresh-")
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(data)
                    os.chmod(staged, 0o600)
                    os.replace(staged, target)
                finally:
                    if os.path.exists(staged):
                        os.unlink(staged)
        result = subprocess.run([helper, "check", name], capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise RuntimeError("The login was imported but the final connection check failed")
        print(json.dumps({"profile": name, "account_id": expected, "refreshed": True,
                          "gateway_verified": True, "default_refreshed": config is not None}))
    finally:
        pending.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Exception text is deliberately controlled; do not expose transport bodies.
        print(json.dumps({"error": type(error).__name__, "message": "Refresh failed; existing backups are retained."}))
        sys.exit(1)
