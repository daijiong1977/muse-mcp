"""Official MCP stdio server exposing a small Muse primitive surface."""

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .client import MuseClient


mcp = FastMCP("muse-mcp")
_client = MuseClient()


def get_client() -> MuseClient:
    """Return the client; each invocation uses an independent Gateway."""
    return _client


@mcp.tool()
def muse_status() -> Dict[str, Any]:
    """Return Muse authentication, VM reachability, identity, sessions, and unread state."""
    return get_client().status()


@mcp.tool()
def muse_threads(include_archived: bool = False) -> Any:
    """List persistent Muse side chats; archived chats are excluded by default."""
    return get_client().threads(include_archived=include_archived)


@mcp.tool()
def muse_send(
    message: str,
    thread: Optional[str] = None,
    wait_seconds: Optional[float] = 90,
    fire_and_forget: bool = False,
) -> Any:
    """Send natural-language work to Muse and optionally wait for its genuine reply.

    Thread may be a session ID or a configured alias. A wait timeout is not a
    workflow limit; long browser tasks may use several minutes or more. Set
    fire_and_forget to return after the gateway accepts the message.
    """
    return get_client().send(
        message=message,
        thread=thread,
        wait_seconds=wait_seconds,
        fire_and_forget=fire_and_forget,
    )


@mcp.tool()
def muse_history(
    thread: Optional[str] = None,
    limit: int = 20,
    raw: bool = False,
) -> Any:
    """Read scoped Muse history with event, reply, sequence, and proactive metadata."""
    return get_client().history(thread=thread, limit=limit, raw=raw)


@mcp.tool()
def muse_feed(limit: Optional[int] = None, raw: bool = False) -> Any:
    """Read Muse feed items with IDs, content, dates, editions, and source metadata."""
    return get_client().feed(limit=limit, raw=raw)


@mcp.tool()
def muse_raw(
    method: str,
    body: Any = None,
    path_params: Optional[Dict[str, Any]] = None,
    query_params: Optional[Dict[str, Any]] = None,
    timeout: float = 30,
) -> Any:
    """Call one validated routes.json gateway method (advanced, unstable API).

    Muse's internal routes are unversioned and may change. Credential-shaped
    fields in route responses are redacted and raw payloads are not logged.
    """
    return get_client().raw(
        method=method,
        body=body,
        path_params=path_params,
        query_params=query_params,
        timeout=timeout,
    )


def main() -> None:
    """Run the server over local stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
