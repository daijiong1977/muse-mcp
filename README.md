# muse-mcp

`muse-mcp` is a local, stdio Model Context Protocol server that lets another
agent delegate natural-language browser, research, and computer-use work to a
personal [Muse](https://muse.ai/) agent. It is intentionally a thin transport
layer: Muse remains responsible for browser interaction and domain decisions.
There is no Instacart, Marketplace, checkout, notification, or workflow logic
in this project.

The server reuses the upstream `muse-cli` authentication, VM discovery, Noise
WebSocket gateway, route table, chat polling, history, and feed implementation.
Muse's gateway is an internal, unversioned API and can break when the Muse web
client changes.

## Install

Use Python 3.10+ and `uv`:

```bash
git clone <this-repository> muse-mcp
cd muse-mcp
uv venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -e .
```

The package installs the `muse-mcp` console command. The repository also keeps
the original `cli.py` entry point for upstream-compatible command-line use.

## Authenticate with Chrome

1. Log in to `https://muse.ai/` in Chrome.
2. Keep a Muse tab open and focused in that Chrome session.
3. Export the browser session:

   ```bash
   .venv/bin/muse-mcp auth export
   # or: .venv/bin/muse-mcp doctor
   ```

Cookies are stored at `~/.config/muse-cli/cookies.txt` with mode `0600`, and
remain compatible with the existing `muse-cli` configuration. Access and
gateway tokens are fetched for each gateway connection; they are not stored.
Re-run `auth export` when authentication expires. Manual Netscape or
`name=value; ...` cookie files are also supported if they contain `hatch_sess`.
The export helper uses `agent-browser` to read the active logged-in Chrome
session.

## Run the MCP server

The default transport is local stdio; there is no listening network socket:

```bash
.venv/bin/muse-mcp
# equivalent:
.venv/bin/python -m muse_mcp
```

For an MCP client, copy `mcp-config.example.json`, replace the absolute path,
and add the `mcpServers.muse` entry to that client's configuration. A typical
entry is:

```json
{
  "mcpServers": {
    "muse": {
      "command": "/Users/me/muse-mcp/.venv/bin/muse-mcp",
      "args": []
    }
  }
}
```

## Tools

- **`muse_status`** — structured authentication, VM ID/reachability/health,
  Muse identity, session count, and unread state.
- **`muse_threads(include_archived=false)`** — persistent side chats with
  `session_id`, title, pinned/archived flags, and update metadata.
- **`muse_send(message, thread?, wait_seconds?, fire_and_forget?)`** — sends
  arbitrary natural-language instructions to the main chat or selected side
  chat. `thread` can be a session ID or an optional alias in the existing
  `~/.config/muse-cli/config.json`, for example
  `"aliases": {"instacart": "session-id-1"}`. Replies are found by scoped
  history polling, not by assuming the live stream shape. A timeout returns a
  structured `TIMEOUT` result while confirming that the message was sent.
- **`muse_history(thread?, limit=20, raw=false)`** — returns event sequence,
  event type, role, message ID, reply relationship, text, timestamp, and the
  redacted raw event metadata. `raw=true` preserves the gateway response.
- **`muse_feed(limit?, raw=false)`** — returns feed unit IDs, titles, content,
  dates, editions, source/URL metadata, and a redacted unit copy. `raw=true`
  exposes the expanded gateway response with credential-shaped fields redacted.
- **`muse_raw(method, body?, path_params?, query_params?, timeout=30)`** — an
  advanced escape hatch for one method validated against `routes.json`. Route
  names and response shapes are unstable. Credential-shaped fields are
  redacted, raw payloads are not logged, and binary bodies are omitted while
  their size is reported.

All errors use structured codes such as `AUTH_REQUIRED`, `VM_UNAVAILABLE`,
`TIMEOUT`, `INVALID_THREAD`, `UNKNOWN_ROUTE`, and `GATEWAY_ERROR`. Authentication
errors explain how to run `muse-mcp auth export` without exposing cookies or
tokens.

Conceptually, a calling agent can select a configured side chat and delegate:

```text
muse_threads()
→ finds "Instacart"

muse_send(
  thread=<instacart session>,
  message="Add blueberries to the list for this week's grocery run."
)
```

Or use a general browser session without any domain-specific MCP code:

```text
muse_send(
  thread=<general browser session>,
  message="Research these three products and compare current prices. Do not purchase anything."
)
```

## Reliability and security

- Each tool operation opens one gateway connection, and a process-wide lock
  serializes complete operations. This deliberately prevents concurrent Noise
  readers from corrupting state or stealing another request's frames. A long
  `muse_send` wait blocks another operation until it completes.
- Genuine replies are discovered by polling scoped history. Assistant events
  with a new sequence and an empty `reply_to_message_id` are replies; proactive
  pushes are not treated as the delegated response.
- Muse VMs can sleep, restart, or be temporarily unavailable. Retry a structured
  `VM_UNAVAILABLE`; `muse-mcp wake` is available for a configured/known VM ID.
- This server does not listen remotely, expose credentials, log cookies/tokens,
  check out carts, place orders, send third-party messages, or make relevance
  notifications. `muse_raw` is powerful and should only be enabled for a
  trusted local calling agent.
- Respect service rate limits. The gateway is reverse-engineered from the Muse
  web bundle; protocol drift requires updating the upstream implementation and
  `docs/PROTOCOL.md` from current behavior rather than guessing.

## Development

Run the mocked MCP-layer tests without a Muse account:

```bash
python -m unittest discover -s tests -v
```

The tests cover thread filtering, scoped sends, genuine-reply selection,
timeouts, history/feed metadata, raw-route validation, redaction, auth/VM
errors, and serialized access. There is no automated live integration test;
for a real smoke check, authenticate first and run `muse-mcp doctor` followed by
an MCP client call to `muse_status`.

## Layout

```text
muse_mcp/          MCP server, serialized adapter, config, setup wrapper
muse.py            upstream HTTPS auth and Noise gateway
cli.py             preserved upstream CLI/auth/export implementation
routes.json        internal gateway method table
desc0.bin/desc1.bin protobuf framing descriptors
docs/PROTOCOL.md   reverse-engineered protocol notes
```
