#!/usr/bin/env bash
# =============================================================================
# install_mcp.sh — Automated installer for Flexisip MCP Server
# =============================================================================

set -euo pipefail

# ── ANSI colours ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}  ▸${RESET} $*"; }
success() { echo -e "${GREEN}  ✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}  ⚠${RESET} $*"; }
abort()   { echo -e "${RED}  ✗ ERROR:${RESET} $*" >&2; exit 1; }
header()  { echo -e "\n${BOLD}${BLUE}┌─ $* ──────────────────────────────────${RESET}\n"; }

# ── Prerequisites ─────────────────────────────────────────────────────────────
header "Checking Prerequisites"

if ! command -v uv &> /dev/null; then
    warn "uv is not installed."
    info "Installing uv via brew (macOS)..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        brew install uv
    else
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi
fi
success "uv is installed"

# ── Project Setup ─────────────────────────────────────────────────────────────
header "Setting up Project"

if [[ ! -f "pyproject.toml" ]]; then
    abort "Please run this script from the project root (where pyproject.toml lives)."
fi

info "Syncing dependencies with uv..."
uv sync
success "Dependencies installed"

# ── SSH Configuration ──────────────────────────────────────────────────────────
header "SSH Configuration"

CONFIG_FILE="$HOME/.ssh/config"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$CONFIG_FILE"

MISSING_SERVERS=()
for srv in stg2 stg2b prod prod2; do
    if ! grep -qE "^Host[[:space:]]+${srv}([[:space:]]|$)" "$CONFIG_FILE" 2>/dev/null; then
        MISSING_SERVERS+=("$srv")
    fi
done

if [[ ${#MISSING_SERVERS[@]} -gt 0 ]]; then
    warn "The following SSH aliases are missing from ~/.ssh/config: ${MISSING_SERVERS[*]}"
    echo ""
    info "The MCP server requires these aliases to open SSH tunnels."
    info "Please add them manually or use the template below:"
    echo ""
    echo -e "${CYAN}"
    for srv in "${MISSING_SERVERS[@]}"; do
        echo "Host $srv"
        echo "    HostName <ip-or-dns>"
        echo "    User <username>"
        echo ""
    done
    echo -e "${RESET}"
else
    success "All Flexisip SSH aliases found in ~/.ssh/config"
fi

# ── MCP Client Configuration ──────────────────────────────────────────────────
header "MCP Client Configuration"

PROJECT_PATH=$(pwd)
CONFIG_JSON=$(cat <<EOF
{
  "mcpServers": {
    "flexisip": {
      "command": "uv",
      "args": [
        "--directory",
        "$PROJECT_PATH",
        "run",
        "flexisip-mcp"
      ]
    }
  }
}
EOF
)

info "To use this server with Claude Desktop, add the following to your config:"
echo ""
echo -e "${YELLOW}Path:${RESET} ~/Library/Application Support/Claude/claude_desktop_config.json"
echo ""
echo -e "${CYAN}$CONFIG_JSON${RESET}"
echo ""

success "Installation complete!"
