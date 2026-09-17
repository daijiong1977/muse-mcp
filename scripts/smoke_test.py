#!/usr/bin/env python3
"""Low-risk authenticated Muse connectivity smoke test.

This intentionally performs no writes: identity, VM health, session listing,
and a small main-chat history read only.
"""

import json
import sys

from cli import connect, load_config
from muse import AuthError, GatewayError, VMUnavailableError
from muse_mcp.client import redact


def main() -> int:
    gateway = None
    try:
        gateway = connect(load_config())
        identity = gateway.call_json("identity")
        health = gateway.call_json("vm.health")
        sessions = gateway.call_json("sessions.list")
        history = gateway.call_json("chat.history", body={"limit": 5})
        result = {
            "ok": True,
            "vm_id": gateway.vm_id,
            "identity": redact(identity),
            "health": redact(health),
            "session_count": len(sessions.get("sessions", [])) if isinstance(sessions, dict) else None,
            "history_count": len(history.get("chat_events", [])) if isinstance(history, dict) else None,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (AuthError, FileNotFoundError):
        print(json.dumps({
            "ok": False,
            "error": {
                "code": "AUTH_REQUIRED",
                "message": "Run `muse-mcp auth export` while logged into muse.ai in Chrome.",
            },
        }))
        return 2
    except VMUnavailableError:
        print(json.dumps({
            "ok": False,
            "error": {"code": "VM_UNAVAILABLE", "message": "Muse VM resolution is temporarily unavailable."},
        }))
        return 3
    except GatewayError as exc:
        print(json.dumps({
            "ok": False,
            "error": {"code": "GATEWAY_ERROR", "status": getattr(exc, "status", None)},
        }))
        return 3
    except TimeoutError:
        print(json.dumps({
            "ok": False,
            "error": {"code": "TIMEOUT", "message": "Muse did not respond during the smoke test."},
        }))
        return 4
    finally:
        if gateway is not None:
            gateway.close()


if __name__ == "__main__":
    sys.exit(main())
