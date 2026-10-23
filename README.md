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

Chrome cookie export requires the external `agent-browser` executable:

```bash
npm i -g agent-browser
```

## Authenticate with Chrome

1. Log in to `https://muse.ai/` in Chrome.
2. Keep a Muse tab open and focused in that Chrome session.
3. Export the browser session:

   ```bash
   .venv/bin/muse-mcp auth export
   .venv/bin/muse-mcp doctor
   ```

`doctor` only checks authentication and Muse status; it does not authenticate
or export cookies. If authentication expires, rerun `muse-mcp auth export`
while logged into Muse, then run `muse-mcp doctor` again.

Cookies are stored at `~/.config/muse-cli/cookies.txt` with mode `0600`, and
remain compatible with the existing `muse-cli` configuration. Access and
gateway tokens are fetched for each gateway connection; they are not stored.
Manual Netscape or `name=value; ...` cookie files are also supported if they
contain `hatch_sess`. The export helper uses `agent-browser` to read the active
logged-in Chrome session.

## Refresh a Linux login from your Mac with one command

`muse-refresh-linux` checks the selected Linux profile over SSH first. If
authentication and its gateway work, it exits without using a browser. For an
expired login (HTTP 401), it reads the currently logged-in Muse session from
ego(lite) or Chrome, checks the account ID, transfers a private temporary cookie file over
SSH, and calls the existing server profile importer. The importer checks the
new account and gateway before saving it and backs up the old profile.

Create a private `~/.config/muse-refresh-linux/config.json` on the machine
running the browser (substitute your own host, SSH key, and existing profile):

```json
{
  "host": "user@example.org",
  "identity_file": "/Users/me/.ssh/server_key",
  "remote_python": "/home/user/.local/share/uv/tools/muse-cli/bin/python",
  "profile": "primary-demo",
  "default_login_profile": "primary-demo",
  "browser": "ego"
}
```

```bash
chmod 600 ~/.config/muse-refresh-linux/config.json
muse-refresh-linux
muse-refresh-linux another-profile
muse-refresh-linux --force
muse-refresh-linux --browser chrome --force
```

The server must already have the compatible `muse-profile` helper installed.
`default_login_profile` is optional: it selects the one profile whose refresh
also updates the unqualified `muse-cli` login. That update is allowed only
when the default and selected profile have identical old cookie files, so
another account's default cannot be silently replaced. Default files are
backed up separately. Omit this setting to refresh named profiles only.

`--force` tests the export/import path even when the Linux login already works.
With `browser: "ego"`, install ego(lite), complete onboarding, sign into the
intended Muse account, and keep the app running. The helper reads Muse in a
background TaskSpace and closes its task tabs afterward; it does not import
the entire Chrome profile or need Chrome's remote-debugging permission. If the
login is missing or wrong, it hands the Space to you for sign-in and stops.
After signing in, rerun with the printed `--resume-ego SPACE_ID` option to
resume that same Space (add `--force` if the server login still works).

With `browser: "chrome"` (the default when omitted), Chrome must be running
with one Muse tab in the intended instance, Muse signed in, and remote debugging
allowed. Wrong accounts are rejected before upload. The local default Muse CLI/MCP credentials are not
changed, and temporary local cookie files are removed when the command exits.
The remote staging file is removed after import, with a best-effort cleanup
if the SSH connection fails; a file left by a dropped connection remains private.
Chrome may display **Allow remote debugging?** on connection, especially after
a restart. Approve it manually; the helper does not disable that safeguard.
The exporter reuses a named agent-browser session instead of creating a new
session for every command.

This command reuses an existing browser login. It does not store passwords,
bypass a fresh login/2FA, or guarantee that a revoked browser session can be
renewed without a person. A headless browser or ego(lite) can retain browser
state, but does not remove those authentication requirements. Use the explicit
browser setting to select the source. For a Linux-only invocation to use a
browser on another computer, it would also need authorized access back to that
computer; this command normally runs on the Mac that owns the browser.

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
- **`muse_send(message, thread?, wait_seconds?, fire_and_forget?, images?)`** — sends
  arbitrary natural-language instructions to the main chat or selected side
  chat. `thread` can be a session ID or an optional alias in the existing
  `~/.config/muse-cli/config.json`, for example
  `"aliases": {"instacart": "session-id-1"}`. Replies are found by scoped
  history polling, not by assuming the live stream shape. A timeout returns a
  structured `TIMEOUT` result while confirming that the message was sent.
  `images` attaches local PNG, JPEG, GIF, or WebP files, in array order,
  alongside the text. Paths refer to the machine running the MCP server.
- **`muse_history(thread?, limit=20, raw=false)`** — returns event sequence,
  event type, role, message ID, reply relationship, text, timestamp, and the
  redacted raw event metadata. `raw=true` preserves the gateway response.
- **`muse_feed(limit?, raw=false)`** — returns feed unit IDs, titles, content,
  dates, editions, source/URL metadata, and a redacted unit copy. `raw=true`
  exposes the expanded gateway response with credential-shaped fields redacted.
- **`muse_raw(method, body?, path_params?, query_params?, timeout=30)`** — an
  advanced escape hatch for one method validated against `routes.json`. It is
  disabled by default; set `MUSE_MCP_ENABLE_RAW=1` (or `"enable_raw": true` in
  the compatible config) only for a trusted local orchestrator. Route names
  and response shapes are unstable. Known credential-shaped fields, bearer
  tokens, and obvious token assignments are redacted; binary bodies are
  omitted while their size is reported.

All errors use structured codes such as `AUTH_REQUIRED`, `VM_UNAVAILABLE`,
`TIMEOUT`, `INVALID_THREAD`, `UNKNOWN_ROUTE`, `RAW_DISABLED`, and
`GATEWAY_ERROR`. Authentication errors explain how to run
`muse-mcp auth export` without exposing cookies or tokens.

## Send local images (CLI or MCP)

The CLI accepts repeatable `--image` options:

```bash
muse-mcp send "Describe this picture" --image /absolute/path/reference.png --wait 90
muse-mcp send "Compare these images" --image ./first.jpg --image ./second.webp --thread SESSION_ID
muse-mcp send --image ./reference.png --wait 90
```

For MCP, call the existing `muse_send` tool:

```json
{
  "message": "Use these reference images to describe the character and setting.",
  "images": ["/absolute/path/character.png", "/absolute/path/setting.jpg"],
  "wait_seconds": 90
}
```

Supply up to 10 images with a combined size of at most 10 MiB before Base64
encoding. This is the client's limit, not a claim about Muse's general upload
limits. Image MIME types are detected from file signatures; files are sent
without resizing or conversion, and Muse performs decoding. URLs, documents,
and video attachments are not supported by this option. An empty `message`
is allowed when images are supplied.

Images are encoded inline in `chat.stream`, using the same image item shape
as the Muse web client. No browser or separate public file hosting is required.
Validation completes before opening a gateway or sending a message. Encoded
image data is not included in send results or validation errors. Sending an
image acts as your Muse account and may use its allowance.

For a named CLI profile, set the compatible configuration directory explicitly:

```bash
MUSE_CONFIG_DIR="$HOME/.config/muse-cli/profiles/PROFILE_NAME" muse-mcp send "Describe this" --image ./reference.png
```

This feature is in this repository's `muse-mcp` command. A separately installed
upstream `muse-cli` or custom `muse-profile` wrapper does not automatically gain
the `--image` option. Text-only calls and existing thread behavior are preserved.

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

- Each MCP operation opens and closes its own Gateway. There is no
  process-wide operation lock, so unrelated history/feed/status calls can run
  while a long send waits. Each Gateway retains its own send/receive locks;
  no Gateway is shared across concurrent MCP calls.
- Genuine replies are discovered by polling scoped history. Assistant events
  with a new sequence and an empty `reply_to_message_id` are replies; proactive
  pushes are not treated as the delegated response.
- For long computer-use jobs, `fire_and_forget=true` dispatches the request and
  closes its Gateway without waiting for completion. It does not provide a
  protocol-level acceptance acknowledgement; check later with `muse_history`.
  A local timeout never cancels Muse's task.
- Muse VMs can sleep, restart, or be temporarily unavailable. Retry a
  structured `VM_UNAVAILABLE`; `muse-mcp wake` is available for a configured or
  known VM ID. Other protocol failures remain `GATEWAY_ERROR`.
- `muse_raw` is disabled by default and should only be enabled for a trusted
  local orchestrator. Known credential-shaped fields, bearer tokens, and
  obvious token assignments are redacted, but arbitrary route content (for
  example file contents) cannot be guaranteed secret-free.
- This server does not listen remotely, check out carts, place orders, send
  third-party messages, or make relevance notifications.
- `muse.py`, `cli.py`, `routes.json`, descriptors, and protocol notes originate
  from `nikships/muse-cli` and should generally remain close to upstream.
  Respect service rate limits and update protocol behavior from current Muse
  observations rather than guessing.

## Development

Run the mocked MCP-layer tests without a Muse account:

```bash
python -m unittest discover -s tests -v
```

After authenticating, run the low-risk live smoke path:

```bash
muse-mcp doctor
python scripts/smoke_test.py
```

The smoke script only connects, fetches identity/status, lists sessions, and
reads a small history window. It does not send messages or mutate Muse state.
There is no automated live integration test.

## Layout

```text
muse_mcp/          MCP server, serialized adapter, config, setup wrapper
muse.py            upstream HTTPS auth and Noise gateway
cli.py             preserved upstream CLI/auth/export implementation
routes.json        internal gateway method table
desc0.bin/desc1.bin protobuf framing descriptors
docs/PROTOCOL.md   reverse-engineered protocol notes
scripts/           optional low-risk live smoke command
```
