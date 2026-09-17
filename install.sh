#!/usr/bin/env bash
# muse-mcp installer: local MCP server plus authentication/setup helpers.
set -u

REPO_URL="${MUSE_MCP_REPO:-${MUSE_CLI_REPO:-https://github.com/nikships/muse-mcp.git}}"
DEST="${MUSE_MCP_DIR:-${MUSE_CLI_DIR:-$HOME/muse-mcp}}"
BIN_DIR="${MUSE_MCP_BIN:-${MUSE_CLI_BIN:-$HOME/bin}}"
BIN_NAME="muse-mcp"
SKILL_DIR="${MUSE_MCP_SKILLS:-${MUSE_CLI_SKILLS:-$HOME/.agents/skills}}"

fail() { echo "install failed: $1" >&2; exit 1; }

command -v git >/dev/null || fail "git not found"
command -v curl >/dev/null || fail "curl not found"

if ! command -v uv >/dev/null; then
  echo "installing uv"
  curl -fsSL https://astral.sh/uv/install.sh | sh || fail "uv install failed"
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null || fail "uv not found after install"

if [ -d "$DEST/.git" ]; then
  echo "updating existing checkout at $DEST"
  git -C "$DEST" pull --ff-only || fail "git pull failed"
else
  echo "cloning into $DEST"
  git clone "$REPO_URL" "$DEST" || fail "git clone failed"
fi

echo "creating virtualenv and installing dependencies with uv"
if [ ! -x "$DEST/.venv/bin/python" ]; then
  uv venv "$DEST/.venv" || fail "uv venv failed"
fi
uv pip install --python "$DEST/.venv/bin/python" -r "$DEST/requirements.txt" \
  || fail "uv pip install failed"
uv pip install --python "$DEST/.venv/bin/python" -e "$DEST" \
  || fail "muse-mcp package install failed"

mkdir -p "$BIN_DIR"
rm -f "$BIN_DIR/$BIN_NAME"
cat > "$BIN_DIR/$BIN_NAME" <<EOF
#!/usr/bin/env bash
exec "$DEST/.venv/bin/python" -m muse_mcp.cli "\$@"
EOF
chmod +x "$BIN_DIR/$BIN_NAME"
echo "launcher installed at $BIN_DIR/$BIN_NAME"

if [ -d "$DEST/skills/muse-cli" ]; then
  mkdir -p "$SKILL_DIR/muse-cli"
  cp -r "$DEST/skills/muse-cli/." "$SKILL_DIR/muse-cli/"
  echo "skill installed at $SKILL_DIR/muse-cli"
fi

echo
echo "Next steps:"
echo "  1. Log in to https://muse.ai/ in Chrome"
echo "  2. $BIN_NAME auth export"
echo "  3. $BIN_NAME doctor"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "NOTE: $BIN_DIR is not on your PATH; add it or run $BIN_DIR/$BIN_NAME" ;;
esac
