"""Local configuration shared by the legacy CLI and muse-mcp."""

import json
import os
import secrets
from typing import Any, Dict


CONFIG_DIR = os.path.expanduser(os.environ.get("MUSE_CONFIG_DIR", "~/.config/muse-cli"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
COOKIES_FILE = os.path.join(CONFIG_DIR, "cookies.txt")


def load_config() -> Dict[str, Any]:
    """Load compatible muse-cli state and persist a device id if needed."""
    cfg: Dict[str, Any] = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if not isinstance(loaded, dict):
            raise ValueError("Muse config must contain a JSON object")
        cfg = loaded

    changed = False
    cfg.setdefault("cookies_file", COOKIES_FILE)
    if "vm_id" not in cfg and os.environ.get("MUSE_VM_ID"):
        cfg["vm_id"] = os.environ["MUSE_VM_ID"]
    if "node_id" not in cfg:
        cfg["node_id"] = os.environ.get("MUSE_NODE_ID") or secrets.token_hex(8)
        changed = True
    if changed:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    return cfg


def resolve_thread(config: Dict[str, Any], thread: Any) -> Any:
    """Resolve an optional configured alias without requiring aliases."""
    if thread is None:
        return None
    if not isinstance(thread, str) or not thread.strip():
        raise ValueError("thread must be a non-empty session ID or alias")
    aliases = config.get("aliases", {})
    if isinstance(aliases, dict) and thread in aliases:
        resolved = aliases[thread]
        if not isinstance(resolved, str) or not resolved.strip():
            raise ValueError("configured thread alias does not contain a session ID")
        return resolved
    return thread
