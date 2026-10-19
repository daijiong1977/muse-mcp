"""Thin, serialized adapter from MCP calls to the existing Muse gateway."""

import json
import re
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional

from cli import is_reply
from muse import AuthError, Gateway, GatewayError, ROUTES, VMUnavailableError, load_cookies

from .config import load_config, resolve_thread
from .attachments import build_message_items


_SECRET_KEY = re.compile(
    r"(?:token|secret|cookie|password|credential|authorization|private_key|api[_-]?key)",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r'(?i)("?(?:access_token|auth_token|hatch_token|api[_-]?key|cookie|password|secret)"?\s*:\s*")([^"\\]*(?:\\.[^"\\]*)*)(")'
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:access_token|auth_token|hatch_token|api[_-]?key|cookie|password|secret)\s*[=:]\s*)[^\s&,;]+"
)
_BEARER = re.compile(r"(?i)(Bearer\s+)[^\s,;]+")


class MCPInputError(ValueError):
    """An invalid MCP argument that should be returned as structured data."""


def _redact_text(value: str) -> str:
    value = _BEARER.sub(r"\1[REDACTED]", value)
    value = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)
    return _SECRET_TEXT.sub(r"\1[REDACTED]\3", value)


def redact(value: Any) -> Any:
    """Redact credential-shaped fields in data returned by an internal route."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SECRET_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _error(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    result["error"].update(extra)
    return result


def _auth_error() -> Dict[str, Any]:
    result = _error(
        "AUTH_REQUIRED",
        "Muse authentication has expired or is missing. Run `muse-mcp auth export` while logged into muse.ai in Chrome.",
    )
    result["authenticated"] = False
    return result


def _exception_error(exc: Exception, thread: Optional[str] = None) -> Dict[str, Any]:
    if isinstance(exc, AuthError):
        return _auth_error()
    if isinstance(exc, (MCPInputError, ValueError, TypeError)):
        return _error("INVALID_ARGUMENT", str(exc))
    if isinstance(exc, TimeoutError):
        return _error("TIMEOUT", "Muse did not respond before the gateway timeout.")
    if isinstance(exc, VMUnavailableError):
        return _error(
            "VM_UNAVAILABLE",
            "Muse's VM resolution is temporarily unavailable. Retry after it becomes available; `muse-mcp wake` can wake a known VM.",
        )
    if isinstance(exc, GatewayError):
        status = getattr(exc, "status", None)
        if thread and status in (400, 404):
            return _error("INVALID_THREAD", "Muse rejected the requested thread.", status=status)
        return _error("GATEWAY_ERROR", "Muse gateway request failed.", status=status)
    if isinstance(exc, (OSError, ConnectionError)):
        return _error("GATEWAY_ERROR", "Muse gateway connection failed.")
    return _error("GATEWAY_ERROR", "Muse gateway operation failed.")


def _updated_time(session: Dict[str, Any]) -> Optional[str]:
    value = session.get("updated_at_ms")
    if isinstance(value, (int, float)) and value:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    for key in ("updated_at", "last_updated", "last_updated_at"):
        if session.get(key) is not None:
            return str(session[key])
    return None


def _unread_count(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        for key in ("unread", "unread_count", "count", "total"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)):
                return int(candidate)
    return None


def _event_row(event: Dict[str, Any]) -> Dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    text = payload.get("display_text") or payload.get("content") or event.get("text") or ""
    role = payload.get("role") or event.get("role") or event.get("event_name")
    row = {
        "seq": event.get("seq"),
        "event_name": event.get("event_name") or event.get("event") or event.get("type"),
        "role": role,
        "message_id": payload.get("message_id") or event.get("message_id"),
        "reply_to_message_id": payload.get("reply_to_message_id") or event.get("reply_to_message_id"),
        "text": text,
        "timestamp": event.get("timestamp") or event.get("created_at") or payload.get("timestamp"),
        "raw": redact(event),
    }
    return row


class MuseClient:
    """Adapter where each invocation owns one independent Gateway instance.

    Gateway send/receive locks still protect one Noise connection. No lock is
    shared across MCP calls, so long-running sends do not block other tools.
    """

    def __init__(
        self,
        config_loader: Callable[[], Dict[str, Any]] = load_config,
        gateway_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._config_loader = config_loader
        self._gateway_factory = gateway_factory or Gateway

    @contextmanager
    def _gateway(self, config: Dict[str, Any]) -> Iterator[Any]:
        cookies_path = config.get("cookies_file")
        try:
            cookies = load_cookies(cookies_path)
        except (OSError, TypeError, AuthError) as exc:
            raise AuthError(f"could not read Muse cookies: {exc}") from exc
        if not cookies.strip():
            raise AuthError("Muse cookies are empty")
        gateway = self._gateway_factory(cookies, vm_id=config.get("vm_id"))
        try:
            yield gateway
        finally:
            try:
                gateway.close()
            except Exception:
                pass

    def _execute(self, operation: Callable[[Any, Dict[str, Any]], Any], thread: Optional[str] = None) -> Any:
        try:
            config = self._config_loader()
            with self._gateway(config) as gateway:
                return operation(gateway, config)
        except Exception as exc:
            return _exception_error(exc, thread=thread)

    def status(self) -> Dict[str, Any]:
        def operation(gateway: Any, _config: Dict[str, Any]) -> Dict[str, Any]:
            sessions = gateway.call_json("sessions.list")
            unread = gateway.call_json("chat.unread_count")
            identity = gateway.call_json("identity")
            try:
                health = gateway.call_json("vm.health")
            except Exception as exc:
                health = {"error": _exception_error(exc)["error"]}
            session_rows = sessions.get("sessions", []) if isinstance(sessions, dict) else []
            return redact({
                "ok": True,
                "authenticated": True,
                "vm_id": gateway.vm_id,
                "vm": {"id": gateway.vm_id, "reachable": True, "health": health},
                "identity": identity,
                "session_count": len(session_rows) if isinstance(session_rows, list) else None,
                "unread": unread,
                "unread_count": _unread_count(unread),
            })

        result = self._execute(operation)
        return result if isinstance(result, dict) else _error("GATEWAY_ERROR", "Invalid status response.")

    def threads(self, include_archived: bool = False) -> Any:
        def operation(gateway: Any, _config: Dict[str, Any]) -> List[Dict[str, Any]]:
            data = gateway.call_json("sessions.list")
            sessions = data.get("sessions", []) if isinstance(data, dict) else []
            rows = []
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                if not include_archived and session.get("archived"):
                    continue
                rows.append({
                    "session_id": session.get("session_id"),
                    "title": session.get("title"),
                    "pinned": bool(session.get("pinned", False)),
                    "archived": bool(session.get("archived", False)),
                    "thread": session.get("is_thread"),
                    "updated_at": _updated_time(session),
                    "updated_at_ms": session.get("updated_at_ms"),
                })
            return redact(rows)

        return self._execute(operation)

    def history(self, thread: Optional[str] = None, limit: int = 20, raw: bool = False) -> Any:
        def operation(gateway: Any, config: Dict[str, Any]) -> Any:
            resolved = resolve_thread(config, thread)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                raise MCPInputError("limit must be a positive integer")
            params: Dict[str, Any] = {"limit": limit}
            if resolved:
                params["session_id"] = resolved
            data = gateway.call_json("chat.history", body=params)
            if raw:
                return redact(data)
            events = data.get("chat_events", []) if isinstance(data, dict) else []
            return redact([_event_row(event) for event in events if isinstance(event, dict)])

        return self._execute(operation, thread=thread)

    def send(
        self,
        message: str,
        thread: Optional[str] = None,
        wait_seconds: Optional[float] = 90,
        fire_and_forget: bool = False,
        images: Optional[List[str]] = None,
    ) -> Any:
        try:
            items = build_message_items(message, images)
        except ValueError as exc:
            return _error("INVALID_ARGUMENT", str(exc))

        def operation(gateway: Any, config: Dict[str, Any]) -> Dict[str, Any]:
            resolved = resolve_thread(config, thread)
            if wait_seconds is not None and (not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool) or wait_seconds < 0):
                raise MCPInputError("wait_seconds must be non-negative or null")
            params: Dict[str, Any] = {
                "items": items,
                "node_id": config["node_id"],
                "capabilities": {},
            }
            if resolved:
                params["session_id"] = resolved
            scope = {"session_id": resolved} if resolved else {}
            baseline = 0
            try:
                history = gateway.call_json("chat.history", body={"limit": 1, **scope})
                events = history.get("chat_events", []) if isinstance(history, dict) else []
                if events:
                    baseline = max((event.get("seq", 0) or 0) for event in events if isinstance(event, dict))
            except TimeoutError:
                # A read timeout is transient; the send can still be dispatched.
                pass
            except GatewayError:
                raise

            stream_id = gateway._open("chat.stream", body=params)
            result: Dict[str, Any] = {"ok": True, "sent": True, "stream_id": stream_id}
            if fire_and_forget or wait_seconds == 0:
                result["waiting"] = False
                result["acknowledged"] = False
                result["note"] = "request dispatched; inspect muse_history for progress or completion"
                return result

            timeout = 90 if wait_seconds is None else float(wait_seconds)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    history = gateway.call_json("chat.history", body={"limit": 10, **scope})
                    events = history.get("chat_events", []) if isinstance(history, dict) else []
                    for event in sorted(
                        (event for event in events if isinstance(event, dict)),
                        key=lambda item: item.get("seq", 0) or 0,
                    ):
                        if is_reply(event, baseline):
                            result["reply"] = _event_row(event)
                            return result
                except TimeoutError:
                    # Polling timeouts are transient; keep waiting until the caller's deadline.
                    pass
                except GatewayError:
                    raise
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(4.0, remaining))
            result["ok"] = False
            result["acknowledged"] = False
            result["error"] = {
                "code": "TIMEOUT",
                "message": "Muse request was dispatched, but no genuine assistant reply arrived before the requested timeout.",
                "wait_seconds": timeout,
            }
            result["waiting"] = True
            return result

        return self._execute(operation, thread=thread)

    def feed(self, limit: Optional[int] = None, raw: bool = False) -> Any:
        def operation(gateway: Any, _config: Dict[str, Any]) -> Any:
            if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
                raise MCPInputError("limit must be a positive integer or null")
            data = gateway.call_json("feed.list", query={"limit": limit} if limit else None)
            if raw:
                return redact(data)
            rows: List[Dict[str, Any]] = []
            days = data.get("days", []) if isinstance(data, dict) else []
            for day in days:
                if not isinstance(day, dict):
                    continue
                for edition in day.get("editions", []) or []:
                    if not isinstance(edition, dict):
                        continue
                    for unit in edition.get("units", []) or []:
                        if not isinstance(unit, dict):
                            continue
                        rows.append({
                            "id": unit.get("unit_id") or unit.get("id"),
                            "unit_id": unit.get("unit_id") or unit.get("id"),
                            "title": unit.get("title"),
                            "content": unit.get("content") or unit.get("body") or unit.get("text"),
                            "date": day.get("local_date") or day.get("date"),
                            "edition": edition.get("kind") or edition.get("edition"),
                            "source": unit.get("source") or unit.get("source_name"),
                            "url": unit.get("url") or unit.get("canonical_url"),
                            "published_at": unit.get("published_at") or unit.get("created_at"),
                            "metadata": redact(unit),
                        })
            return redact(rows[:limit] if limit else rows)

        return self._execute(operation)

    def raw(
        self,
        method: str,
        body: Any = None,
        path_params: Optional[Dict[str, Any]] = None,
        query_params: Optional[Dict[str, Any]] = None,
        timeout: float = 30,
    ) -> Any:
        try:
            config = self._config_loader()
        except Exception as exc:
            return _exception_error(exc)
        raw_flag = os.environ.get("MUSE_MCP_ENABLE_RAW")
        enabled = (
            raw_flag.strip().lower() in ("1", "true", "yes", "on")
            if raw_flag is not None
            else bool(config.get("enable_raw", False))
        )
        if not enabled:
            return _error(
                "RAW_DISABLED",
                "muse_raw is disabled. Set MUSE_MCP_ENABLE_RAW=1 for this trusted local MCP server.",
            )
        if not isinstance(method, str) or not method:
            return _error("UNKNOWN_ROUTE", "A route method name is required.")
        if method not in ROUTES:
            return _error("UNKNOWN_ROUTE", "Unknown Muse route; choose a method from routes.json.", method=method)
        if path_params is not None and not isinstance(path_params, dict):
            return _error("INVALID_ARGUMENT", "path_params must be an object.")
        if query_params is not None and not isinstance(query_params, dict):
            return _error("INVALID_ARGUMENT", "query_params must be an object.")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            return _error("INVALID_ARGUMENT", "timeout must be a positive number.")

        def operation(gateway: Any, _config: Dict[str, Any]) -> Dict[str, Any]:
            payload = gateway.request(
                method,
                path_params=path_params,
                body=body,
                query=query_params,
                timeout=float(timeout),
            )
            if isinstance(payload, bytes):
                try:
                    text = payload.decode("utf-8")
                except UnicodeDecodeError:
                    return {
                        "ok": True,
                        "method": method,
                        "binary": True,
                        "size": len(payload),
                        "note": "binary response body omitted for safety",
                    }
                try:
                    decoded = json.loads(text) if text else {}
                    return {"ok": True, "method": method, "result": redact(decoded)}
                except json.JSONDecodeError:
                    return {"ok": True, "method": method, "result": _redact_text(text)}
            return {"ok": True, "method": method, "result": redact(payload)}

        return self._execute(operation)
