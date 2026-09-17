# Repository Guidelines

## Project Overview

`muse-cli` is a small Python 3 command-line client for a personal `muse.ai` agent. It supports chat, side sessions, live events, feed, goals, ideas, unread state, and session management without a browser after a one-time Chrome cookie export. It talks to the service's internal, unversioned gateway protocol directly; protocol drift is expected and must be handled by re-deriving behavior from a current web bundle rather than guessing.

## Architecture & Data Flow

The repository has two root-level Python entry points/modules:

- `cli.py` owns `argparse` command wiring, local configuration, cookie export, JSON formatting, and user-facing exit codes.
- `muse.py` owns cookie parsing, HTTPS authentication/session discovery, Noise transport, protobuf framing, gateway requests, and subscriptions.

Runtime flow:

1. `cli.py` reads `~/.config/muse-cli/config.json` and the configured cookie jar (`~/.config/muse-cli/cookies.txt` by default). A random `node_id` is generated and persisted on first run; `MUSE_VM_ID` and `MUSE_NODE_ID` can supply values.
2. `muse.py` calls `muse.ai` over Chrome-impersonated HTTPS to obtain an access token, hatch token, and assigned VM information.
3. `Gateway` connects to the shared `wss://hatch.metaaivm.com/v1/noise` endpoint using `Noise_XX_25519_AESGCM_SHA256`.
4. Requests use protobuf descriptors from `desc0.bin`/`desc1.bin`, route metadata from `routes.json`, and encrypted `NoiseTransportFrame` service envelopes.
5. Unary responses are decoded as JSON; subscription bodies are newline-delimited JSON records. CLI commands print structured JSON, with a few setup/raw-response paths printing text.

`Gateway` uses synchronous I/O, a send lock, and a receive lock because Noise decryption is stateful. Subscription reading uses one daemon reader thread and a queue. Keep one logical frame consumer per connection; concurrent consumers can misroute responses even though receive calls are serialized.

## Key Directories

- `./` — source entry point (`cli.py`), transport module (`muse.py`), route/protobuf assets, installer, and dependency list.
- `docs/` — reverse-engineered protocol notes and server quirks (`docs/PROTOCOL.md`).
- `skills/muse-cli/` — agent-facing installation, authentication, command, and safety runbook (`SKILL.md`).
- `assets/` — README artwork only; not runtime inputs.

There is no package-style `src/` tree or build output directory. `routes.json`, `desc0.bin`, and `desc1.bin` are runtime protocol inputs, not disposable generated build artifacts.

## Development Commands

Install the documented development/runtime environment with `uv`:

```bash
uv venv
uv pip install -r requirements.txt
uv run cli.py --help
```

The supported installer is:

```bash
curl -fsSL https://raw.githubusercontent.com/nikships/muse-cli/main/install.sh | bash
```

`install.sh` requires `git` and `curl`, bootstraps `uv` if needed, creates `$HOME/muse-cli/.venv`, installs requirements, writes a `$HOME/bin/muse-cli` launcher, and copies the skill. It accepts `MUSE_CLI_REPO`, `MUSE_CLI_DIR`, `MUSE_CLI_BIN`, and `MUSE_CLI_SKILLS` overrides.

After logging in to `https://muse.ai/` in Chrome, the practical smoke path is:

```bash
uv run cli.py auth export
uv run cli.py status
uv run cli.py threads
uv run cli.py history --limit 5
uv run cli.py send "message" --wait 120
```

Use the installed launcher as `muse-cli ...`. `raw <method> --body '{}' [--param k=v]` is the escape hatch for any route in `routes.json`; prefer named commands when one exists. There is no separate build step, Makefile, task runner, or package metadata.

## Code Conventions & Common Patterns

- Keep the root-level Python script/module layout. `cli.py` command handlers use `cmd_<operation>` names and are registered in `main()` with `argparse` subparsers.
- Use small synchronous functions and the existing `Gateway` abstraction rather than adding a second transport/client layer. Commands generally call `connect(load_config())`, emit via `out(...)`, and close the gateway in `finally`.
- Use `Gateway.request()` for raw bytes/status handling, `call_json()` for JSON API results, and `subscribe_raw()`/`subscribe_json()` for NDJSON streams. Route lookup and path substitution must continue to come from `routes.json`.
- Preserve explicit error behavior: `AuthError` exits 2, `GatewayError` exits 3, and `TimeoutError` exits 4. Gateway HTTP failures become `GatewayError` with bounded response detail; malformed JSON is also treated as a gateway error.
- Preserve protocol safety: quote route path parameters, send GET request bodies as query parameters, retain request IDs/content types, and do not create multiple logical readers on one Noise connection.
- Fetch access and gateway tokens on every run. Only local cookies/config state persists; never commit cookie jars, config, HAR captures, or generated Python caches (see `.gitignore`).
- Keep server-specific method shapes and quirks documented beside the implementation in `docs/PROTOCOL.md`; do not “fix” a failing internal call by inventing a new payload or crypto mode.
- Preserve the CLI's user-facing JSON shape and command names unless the change explicitly requires a contract change. Write operations (`send`, reactions, idea execution, and session mutations) act as the user and should be announced/handled deliberately.

## Important Files

- `cli.py` — executable entry point, all command handlers, local config/cookie paths, browser export, parser, and exit-code mapping.
- `muse.py` — `Gateway`, authentication helpers, protobuf descriptor loading, route-driven request/response transport, and subscriptions.
- `routes.json` — 258 extracted gateway methods with HTTP verbs, paths, services, and request/stream metadata.
- `desc0.bin`, `desc1.bin` — protobuf `FileDescriptorProto` inputs loaded by `muse.py`.
- `requirements.txt` — direct runtime dependencies: `curl_cffi`, `noiseprotocol`, and `protobuf`.
- `install.sh` — uv-based checkout/dependency/launcher/skill installer.
- `docs/PROTOCOL.md` — protocol derivation and live server quirks; update when a route or wire behavior changes.
- `skills/muse-cli/SKILL.md` — operational instructions for agents using the CLI.
- `README.md` — user-facing setup, command reference, and contribution notes.

## Runtime/Tooling Preferences

- Use Python 3 with `uv` for virtualenv and dependency installation. No Python minimum version is declared; avoid relying on newer syntax or APIs without checking the supported environment.
- Run the script directly with `uv run cli.py ...` or through the generated `muse-cli` wrapper. `muse` is intentionally avoided because it can clash with Muse Code.
- Required direct dependencies are `curl_cffi>=0.11`, `noiseprotocol>=0.3`, and `protobuf>=5.0`; there is no lockfile or package/build manifest.
- `auth export` uses `agent-browser` against a running Chrome tab focused on `muse.ai`; normal authenticated commands do not need a browser. Manual cookie jars may be Netscape format or `name=value; ...` and must include `hatch_sess`.
- Local auth state belongs under `~/.config/muse-cli`; cookies should remain mode 600. Do not log or commit secrets.

## Testing & QA

No automated test suite, test directory, CI workflow, coverage configuration, lint configuration, or formatter configuration is present. Do not claim test coverage that does not exist.

Use manual smoke checks against the actual CLI instead:

```bash
command -v muse-cli
muse-cli --help >/dev/null && echo cli-ok
muse-cli auth export
test -s ~/.config/muse-cli/cookies.txt && echo auth-ok
muse-cli status
```

`status` is the end-to-end authenticated check. For changes to command behavior, exercise the affected command and verify its JSON output and documented exit behavior. For transport/protocol changes, use a live authenticated call and update `docs/PROTOCOL.md` with observed server errors or method-shape changes. Respect service rate limits and avoid looping write operations.
