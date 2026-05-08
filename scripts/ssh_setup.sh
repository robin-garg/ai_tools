#!/usr/bin/env bash
# =============================================================================
# ssh_setup.sh — Interactive SSH key setup for GitHub on macOS
# Part of ai_tools  (https://github.com/netsmartz/ai_tools)
#
# Sets up ONE GitHub account per run. Run it again for a second account.
#
# What this script does:
#   1. Asks for your email, key suffix, and host alias
#   2. Generates an Ed25519 SSH key with a passphrase you choose
#   3. Starts the SSH agent and adds the key to macOS Keychain (enter once, never again)
#   4. Safely appends a Host block to ~/.ssh/config (skips if alias already exists)
#   5. Copies the public key to clipboard and walks you through adding it to GitHub
#   6. Tests the connection and prints a quick-reference summary
#
# Usage:
#   bash scripts/ssh_setup.sh
# =============================================================================

set -euo pipefail

# ── ANSI colours ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

# ── Helper functions ──────────────────────────────────────────────────────────
info()    { echo -e "${CYAN}  ▸${RESET} $*"; }
success() { echo -e "${GREEN}  ✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}  ⚠${RESET} $*"; }
abort()   { echo -e "${RED}  ✗ ERROR:${RESET} $*" >&2; exit 1; }
header()  { echo -e "\n${BOLD}${BLUE}┌─ $* ──────────────────────────────────${RESET}\n"; }
divider() { echo -e "${BLUE}────────────────────────────────────────────────${RESET}"; }

prompt() {
    # prompt <variable_name> <question> [default]
    local -n _ref=$1
    local question="$2"
    local default="${3:-}"
    if [[ -n "$default" ]]; then
        read -rp "  ${question} [${default}]: " _ref
        _ref="${_ref:-$default}"
    else
        read -rp "  ${question}: " _ref
        [[ -n "$_ref" ]] || abort "This field is required."
    fi
}

# ── Welcome screen ────────────────────────────────────────────────────────────
clear
echo -e "${BOLD}${BLUE}"
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║        SSH Key Setup for GitHub  —  macOS        ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo -e "${RESET}"
echo "  This script is fully interactive and guides you through"
echo "  every step. The only two things you do manually:"
echo ""
echo -e "    ${BOLD}①${RESET} Type a passphrase when ssh-keygen asks (by design — keeps it secure)"
echo -e "    ${BOLD}②${RESET} Paste the public key into GitHub (script copies it to clipboard for you)"
echo ""
divider
read -rp $'\n  Press Enter to begin...\n'

# ── Gather account info ───────────────────────────────────────────────────────
header "Account Setup"

prompt EMAIL      "Email address"
prompt KEY_SUFFIX "Key name suffix  (key will be saved as id_ed25519_<suffix>)" "company"
KEY_NAME="id_ed25519_${KEY_SUFFIX}"
prompt ALIAS      "Host alias for ~/.ssh/config  (used as: Host <alias>)" "github_company"

# ── Confirmation ──────────────────────────────────────────────────────────────
header "Review — Check Before Proceeding"
echo    "    Email  : $EMAIL"
echo    "    Key    : ~/.ssh/$KEY_NAME"
echo    "    Alias  : Host $ALIAS  →  github.com"
echo ""
read -rp "  Everything look correct? [y/N] " CONFIRM
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "  Aborted. No changes made."; exit 0; }

# ── Ensure ~/.ssh exists ──────────────────────────────────────────────────────
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

# ── Step 1: Generate SSH key ──────────────────────────────────────────────────
header "Step 1 of 5 — Generate SSH Key"

KEY_PATH="$HOME/.ssh/$KEY_NAME"

if [[ -f "$KEY_PATH" ]]; then
    warn "Key already exists: $KEY_PATH"
    warn "Skipping generation. Delete it first if you want to regenerate."
else
    echo ""
    info "Generating key for ${BOLD}$EMAIL${RESET}"
    info "Algorithm : Ed25519 (modern, secure, recommended over RSA)"
    info "Saved to  : $KEY_PATH"
    echo ""
    echo -e "  ${YELLOW}You will now be asked to enter a passphrase.${RESET}"
    echo    "  Choose something strong — you will only type it once"
    echo    "  (macOS Keychain remembers it after that)."
    echo ""
    ssh-keygen -t ed25519 -C "$EMAIL" -f "$KEY_PATH"
    chmod 600 "$KEY_PATH"
    chmod 644 "${KEY_PATH}.pub"
    echo ""
    success "Key pair created:"
    info "  Private : $KEY_PATH  ${RED}← never share this${RESET}"
    info "  Public  : ${KEY_PATH}.pub  ${GREEN}← this goes to GitHub${RESET}"
fi


# ── Step 2: SSH agent + macOS Keychain ───────────────────────────────────────
header "Step 2 of 5 — SSH Agent & macOS Keychain"

info "Starting SSH agent..."
# On macOS the system agent is usually already running; this is a no-op if so.
eval "$(ssh-agent -s)" > /dev/null 2>&1 || true
success "SSH agent ready"
echo ""
echo -e "  ${BOLD}Why this matters:${RESET}"
echo    "  ssh-add --apple-use-keychain stores your passphrase in the macOS Keychain."
echo    "  After this step you will never be asked for it again — not on reboot,"
echo    "  not on new terminal sessions. The agent fetches it automatically."
echo ""

if [[ -f "$KEY_PATH" ]]; then
    info "Adding $KEY_NAME to SSH agent + macOS Keychain..."
    echo -e "  ${YELLOW}Enter the passphrase you just created for this key:${RESET}"
    echo ""
    ssh-add --apple-use-keychain "$KEY_PATH"
    echo ""
    success "$KEY_NAME added — passphrase stored in Keychain"
else
    warn "Key not found: $KEY_PATH — skipping agent step."
fi

# ── Step 3: ~/.ssh/config ─────────────────────────────────────────────────────
header "Step 3 of 5 — Update ~/.ssh/config"

CONFIG_FILE="$HOME/.ssh/config"
touch "$CONFIG_FILE"
chmod 600 "$CONFIG_FILE"

if grep -qE "^Host[[:space:]]+${ALIAS}([[:space:]]|$)" "$CONFIG_FILE" 2>/dev/null; then
    warn "Host '${ALIAS}' already exists in ~/.ssh/config — skipping."
    warn "Remove that block manually if you want to replace it."
else
    cat >> "$CONFIG_FILE" << EOF

# Added by ssh_setup.sh on $(date '+%Y-%m-%d')
Host ${ALIAS}
    HostName github.com
    User git
    IdentityFile ~/.ssh/${KEY_NAME}
    AddKeysToAgent yes
    UseKeychain yes
EOF
    success "Added 'Host ${ALIAS}' block to ~/.ssh/config"
fi

echo ""
info "Your ~/.ssh/config (full file):"
echo ""
echo -e "${CYAN}"
cat "$CONFIG_FILE"
echo -e "${RESET}"


# ── Step 4: Copy public keys → GitHub ────────────────────────────────────────
header "Step 4 of 5 — Add Public Keys to GitHub"

PUB_PATH="${KEY_PATH}.pub"

if [[ -f "$PUB_PATH" ]]; then
    echo ""
    echo -e "  ${YELLOW}Public key (also copied to clipboard):${RESET}"
    echo ""
    echo -e "  ${CYAN}$(cat "$PUB_PATH")${RESET}"
    echo ""
    pbcopy < "$PUB_PATH"
    success "Copied to clipboard!"
    echo ""
    echo -e "  ${BOLD}Steps to add this key to GitHub:${RESET}"
    echo ""
    echo    "  1. The GitHub page is opening in your browser now..."
    echo    "     (log in with the account for $EMAIL if needed)"
    echo    "  2. Title  →  $(hostname -s) — $KEY_NAME"
    echo    "  3. Key    →  Paste  (⌘V)  — already in your clipboard"
    echo    "  4. Click  →  'Add SSH Key'"
    echo ""
    open "https://github.com/settings/ssh/new" 2>/dev/null || \
        info "Open manually: https://github.com/settings/ssh/new"
    echo ""
    read -rp "  Press Enter once you have added the key to GitHub..."
    echo ""
else
    warn "Public key not found: $PUB_PATH — skipping GitHub step."
fi

# ── Step 5: Test connections ──────────────────────────────────────────────────
header "Step 5 of 5 — Test GitHub Connections"

info "Testing:  ssh -T git@${ALIAS}"
echo ""
# GitHub always exits with code 1 even on success — capture output instead.
SSH_TEST=$(ssh -o StrictHostKeyChecking=accept-new -T "git@${ALIAS}" 2>&1 || true)

if echo "$SSH_TEST" | grep -qi "successfully authenticated"; then
    success "git@${ALIAS}  →  connected!"
    echo -e "  ${CYAN}${SSH_TEST}${RESET}"
else
    warn "Unexpected response for git@${ALIAS}:"
    echo -e "  ${YELLOW}  ${SSH_TEST}${RESET}"
    echo ""
    warn "This usually means the key hasn't reached GitHub yet."
    warn "After adding it, you can re-test with:"
    echo -e "  ${CYAN}  ssh -T git@${ALIAS}${RESET}"
fi
echo ""

# ── Final summary ─────────────────────────────────────────────────────────────
header "All Done!"

echo -e "  ${GREEN}${BOLD}SSH setup complete.${RESET}  Here's your quick-reference card:"
echo ""
echo -e "  ${BOLD}Key generated${RESET}"
echo    "    ~/.ssh/$KEY_NAME   ($EMAIL)"
echo ""
echo -e "  ${BOLD}Clone a repo${RESET}"
echo    "    git clone git@${ALIAS}:org-or-user/repo.git"
echo ""
echo -e "  ${BOLD}Set commit identity (per repo, no --global)${RESET}"
echo    "    git config user.email \"$EMAIL\""
echo ""
echo -e "  ${BOLD}Update an existing repo's remote${RESET}"
echo    "    git remote set-url origin git@${ALIAS}:org/repo.git"
echo ""
echo -e "  ${BOLD}Useful commands${RESET}"
echo    "    ssh-add -l                # list all keys in agent"
echo    "    ssh -T git@${ALIAS}       # re-test this connection"
echo    "    cat ~/.ssh/config         # view full SSH config"
echo ""
echo -e "  ${BOLD}Need another account?${RESET}"
echo    "    Run this script again with a different suffix and alias."
echo ""
divider
echo ""
