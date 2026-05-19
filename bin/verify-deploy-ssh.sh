#!/usr/bin/env bash
# Verify that the GitHub Actions deploy-vps.yml workflow's SSH path works
# end-to-end. Runs each pre-condition the workflow depends on as an isolated
# check with a PASS/FAIL line and a remediation hint on failure.
#
# Usage (local machine, with the deploy private key file):
#   bash bin/verify-deploy-ssh.sh /path/to/gha_deploy
#
# Usage (CI dry-run, with the key in an env var):
#   echo "$VPS_SSH_KEY" > /tmp/gha_deploy && chmod 600 /tmp/gha_deploy
#   bash bin/verify-deploy-ssh.sh /tmp/gha_deploy
#
# Exit codes:
#   0  — all checks passed; deploy-vps.yml should work
#   1  — usage error (missing args)
#   2+ — specific check failed (see the FAIL line + remediation hint)

set -uo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
HOST="${VPS_HOST:-scheduler.paynepew.dev}"
USER="${VPS_USER:-deploy}"
PORT="${VPS_PORT:-22}"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/task_scheduler_mcp}"
IMAGE="${IMAGE:-ghcr.io/paynepew/task_scheduler_mcp:latest}"

KEY_FILE="${1:-}"

# ── Output helpers ────────────────────────────────────────────────────────────
GREEN=$'\033[0;32m'
RED=$'\033[0;31m'
YELLOW=$'\033[0;33m'
RESET=$'\033[0m'

pass() { printf "%s✓ PASS%s  %s\n" "$GREEN" "$RESET" "$1"; }
fail() { printf "%s✗ FAIL%s  %s\n" "$RED" "$RESET" "$1"; }
hint() { printf "%s        → %s%s\n" "$YELLOW" "$1" "$RESET"; }

# ── 0. Usage ──────────────────────────────────────────────────────────────────
if [[ -z "$KEY_FILE" ]]; then
    echo "Usage: $0 <path-to-private-key>" >&2
    echo "" >&2
    echo "Optional env vars:" >&2
    echo "  VPS_HOST       (default: scheduler.paynepew.dev)" >&2
    echo "  VPS_USER       (default: deploy)" >&2
    echo "  VPS_PORT       (default: 22)" >&2
    echo "  DEPLOY_DIR     (default: /opt/task_scheduler_mcp)" >&2
    echo "  IMAGE          (default: ghcr.io/paynepew/task_scheduler_mcp:latest)" >&2
    exit 1
fi

echo "Target: ${USER}@${HOST}:${PORT}"
echo "Key:    ${KEY_FILE}"
echo "Deploy: ${DEPLOY_DIR}"
echo "Image:  ${IMAGE}"
echo "──────────────────────────────────────────────────"

# ── 1. Key file exists and looks like a private key ───────────────────────────
if [[ ! -f "$KEY_FILE" ]]; then
    fail "Key file not found: $KEY_FILE"
    hint "Check the path. From CI: echo \"\$VPS_SSH_KEY\" > /tmp/gha_deploy"
    exit 2
fi

if ! head -1 "$KEY_FILE" | grep -qE "^-----BEGIN (OPENSSH|RSA|EC) PRIVATE KEY-----"; then
    fail "Key file does not look like a private key (missing PEM header)"
    hint "Check first line — should be '-----BEGIN OPENSSH PRIVATE KEY-----'"
    hint "Did you accidentally save the public key (.pub) instead?"
    exit 3
fi

# Fix permissions if needed (SSH refuses keys with too-open perms)
chmod 600 "$KEY_FILE" 2>/dev/null || true
pass "Key file readable and looks like a private key"

# ── 2. DNS resolves ───────────────────────────────────────────────────────────
if ! getent hosts "$HOST" >/dev/null 2>&1 && ! nslookup "$HOST" >/dev/null 2>&1; then
    fail "DNS lookup failed for $HOST"
    hint "Check Cloudflare DNS record exists: dig +short $HOST"
    hint "If using IP directly, set VPS_HOST=<ip>"
    exit 4
fi
RESOLVED_IP=$(getent hosts "$HOST" 2>/dev/null | awk '{print $1}' | head -1)
pass "DNS resolves: $HOST → ${RESOLVED_IP:-<see nslookup>}"

# ── 3. TCP reachability on port 22 ────────────────────────────────────────────
# Use nc with 5s timeout; covers Lightsail-firewall / ufw issues.
if command -v nc >/dev/null 2>&1; then
    if nc -z -w 5 "$HOST" "$PORT" 2>/dev/null; then
        pass "TCP port $PORT reachable from this host"
    else
        fail "Cannot reach $HOST:$PORT (TCP timeout or refused)"
        hint "Lightsail console → instance → Networking → IPv4 Firewall:"
        hint "  SSH (port 22) must allow 'Any IPv4 address' for GH Actions to connect."
        hint "  Restricted-IP rules block CI runners (their IPs are dynamic)."
        hint "VPS-side check: 'sudo ufw status' should show '22/tcp ALLOW Anywhere'"
        exit 5
    fi
else
    printf "%s⚠ SKIP%s  TCP reachability (no nc installed)\n" "$YELLOW" "$RESET"
fi

# ── 4. SSH auth as the deploy user ────────────────────────────────────────────
SSH_OPTS="-i $KEY_FILE -p $PORT -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
SSH_CMD="ssh $SSH_OPTS ${USER}@${HOST}"

if ! $SSH_CMD "echo auth-ok" 2>/tmp/ssh-err | grep -q auth-ok; then
    fail "SSH authentication failed"
    hint "Failure reason from SSH:"
    sed 's/^/             /' /tmp/ssh-err >&2
    hint "Check on VPS: 'sudo ls -la /home/deploy/.ssh/' should show authorized_keys"
    hint "Public half of key must be in: /home/deploy/.ssh/authorized_keys"
    hint "Common cause: pasted public key (.pub) into authorized_keys but forgot"
    hint "  to add the private half to GitHub Secrets (or vice versa)."
    exit 6
fi
pass "SSH authentication as $USER works"
rm -f /tmp/ssh-err

# ── 5. deploy user has access to DEPLOY_DIR ───────────────────────────────────
if ! $SSH_CMD "cd $DEPLOY_DIR && pwd" >/dev/null 2>&1; then
    fail "$USER cannot cd into $DEPLOY_DIR"
    hint "On VPS: 'sudo ls -ld $DEPLOY_DIR' should show 'drwxr-x--- deploy deploy'"
    hint "Fix: sudo chown -R deploy:deploy $DEPLOY_DIR && sudo chmod 750 $DEPLOY_DIR"
    exit 7
fi
pass "$USER can access $DEPLOY_DIR"

# ── 6. docker is callable by deploy user ──────────────────────────────────────
if ! $SSH_CMD "docker version --format '{{.Client.Version}}'" >/dev/null 2>&1; then
    fail "$USER cannot run docker (not in docker group?)"
    hint "On VPS: 'sudo groups deploy' should include 'docker'"
    hint "Fix: sudo usermod -aG docker deploy && (re-login the deploy session)"
    exit 8
fi
pass "$USER can run docker"

# ── 7. docker compose is available ────────────────────────────────────────────
if ! $SSH_CMD "cd $DEPLOY_DIR && docker compose version" >/dev/null 2>&1; then
    fail "docker compose (v2) not callable in $DEPLOY_DIR"
    hint "Check 'docker compose version' on VPS as deploy user"
    hint "Compose v2 ships as docker plugin; install via 'apt install docker-compose-plugin'"
    exit 9
fi
pass "docker compose v2 available"

# ── 8. GHCR image pullable (the actual deploy step's prerequisite) ────────────
if ! $SSH_CMD "docker pull $IMAGE" >/dev/null 2>&1; then
    fail "Cannot pull $IMAGE on VPS"
    hint "Check the package's visibility: github.com/users/<owner>/packages/container/<name>"
    hint "Should be Public, OR deploy user must 'docker login ghcr.io' with PAT"
    hint "Also check 'Manage Actions access' on the package: link to PaynePew/task_scheduler_mcp"
    exit 10
fi
pass "$IMAGE pullable on VPS"

# ── Done ──────────────────────────────────────────────────────────────────────
echo "──────────────────────────────────────────────────"
printf "%sAll 8 checks passed.%s deploy-vps.yml should work end-to-end.\n" "$GREEN" "$RESET"
