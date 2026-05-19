#!/usr/bin/env bash
# Idempotent bootstrap for a fresh Ubuntu 24.04 Lightsail Tokyo instance.
# Run as root. Safe to re-run on an already-provisioned VPS — every step is
# guarded so re-runs are no-ops.
#
# Usage: curl -fsSL https://raw.githubusercontent.com/PaynePew/task_scheduler_mcp/main/bin/setup-vps.sh | sudo bash
# Or after cloning: sudo bash bin/setup-vps.sh
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/PaynePew/task_scheduler_mcp.git"
DEPLOY_DIR="/opt/task_scheduler_mcp"
DEPLOY_USER="deploy"
INFRA_SRC="infra/vps"
ELASTICMQ_CONF="elasticmq.conf"

log() { echo "[setup-vps] $*"; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "Error: must run as root (use: sudo bash $0)" >&2
        exit 1
    fi
}

# ── 1. System packages ────────────────────────────────────────────────────────
install_packages() {
    log "Updating apt and installing dependencies..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq \
        ca-certificates \
        curl \
        gnupg \
        lsb-release \
        ufw \
        fail2ban \
        unattended-upgrades \
        apt-transport-https \
        software-properties-common \
        netcat-openbsd
}

# ── 2. Docker Engine + Compose v2 ─────────────────────────────────────────────
install_docker() {
    if command -v docker &>/dev/null; then
        log "Docker already installed ($(docker --version)), skipping."
        return
    fi
    log "Installing Docker Engine..."
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu \
        $(lsb_release -cs) stable" \
        | tee /etc/apt/sources.list.d/docker.list >/dev/null
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
}

# ── 3. Caddy ──────────────────────────────────────────────────────────────────
# Caddy is fully containerised (see compose stack + ADR-028/ADR-029); no host
# installation is performed.

# ── 4. Docker daemon hardening ────────────────────────────────────────────────
configure_docker_daemon() {
    local cfg="/etc/docker/daemon.json"
    local target='{"log-driver":"local","log-opts":{"max-size":"10m","max-file":"3"}}'
    if [[ -f "$cfg" ]] && python3 -c "
import json, sys
d = json.load(open('$cfg'))
ok = (d.get('log-driver') == 'local'
      and d.get('log-opts', {}).get('max-size') == '10m'
      and d.get('log-opts', {}).get('max-file') == '3')
sys.exit(0 if ok else 1)
" 2>/dev/null; then
        log "Docker daemon already configured, skipping."
        return
    fi
    log "Configuring Docker daemon (log driver: local, max 10m/3)..."
    mkdir -p /etc/docker
    echo "$target" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin), indent=2))" \
        > "$cfg"
    systemctl reload-or-restart docker
}

# ── 5. deploy user ────────────────────────────────────────────────────────────
create_deploy_user() {
    if id "$DEPLOY_USER" &>/dev/null; then
        log "User '$DEPLOY_USER' already exists, skipping."
    else
        log "Creating user '$DEPLOY_USER'..."
        useradd -m -s /bin/bash "$DEPLOY_USER"
    fi
    if ! groups "$DEPLOY_USER" | grep -q docker; then
        usermod -aG docker "$DEPLOY_USER"
        log "Added '$DEPLOY_USER' to docker group."
    fi
}

# ── 6. Clone / update repo ────────────────────────────────────────────────────
setup_repo() {
    if [[ -d "$DEPLOY_DIR/.git" ]]; then
        log "Repo already cloned at $DEPLOY_DIR, pulling latest..."
        # Repo is owned by $DEPLOY_USER (set on first run); pull as that user
        # so git's safe.directory check doesn't reject root running on a
        # deploy-owned working tree.
        sudo -u "$DEPLOY_USER" git -C "$DEPLOY_DIR" pull --ff-only
    else
        log "Cloning repo to $DEPLOY_DIR..."
        git clone --depth 1 "$REPO_URL" "$DEPLOY_DIR"
    fi
    chown -R "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_DIR"
    chmod 750 "$DEPLOY_DIR"

    # Ensure .env exists (secrets must be filled manually)
    if [[ ! -f "$DEPLOY_DIR/.env" ]]; then
        cp "$DEPLOY_DIR/$INFRA_SRC/.env.example" "$DEPLOY_DIR/.env"
        chown "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_DIR/.env"
        chmod 600 "$DEPLOY_DIR/.env"
        log "WARNING: $DEPLOY_DIR/.env created from .env.example — fill in secrets before starting."
    fi

    # Copy infra/vps files into deploy dir root (idempotent).
    # elasticmq.conf already lives at repo root after clone, so no copy needed.
    cp "$DEPLOY_DIR/$INFRA_SRC/docker-compose.yml" "$DEPLOY_DIR/docker-compose.yml"
    cp "$DEPLOY_DIR/$INFRA_SRC/Caddyfile" "$DEPLOY_DIR/Caddyfile"
    # Sync static landing-page assets so Caddy's ./static:/var/www mount resolves.
    rsync -a --delete "$DEPLOY_DIR/$INFRA_SRC/static/" "$DEPLOY_DIR/static/"
    chown -R "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_DIR/static"
    chown "$DEPLOY_USER:$DEPLOY_USER" \
        "$DEPLOY_DIR/docker-compose.yml" \
        "$DEPLOY_DIR/Caddyfile" \
        "$DEPLOY_DIR/$ELASTICMQ_CONF"
}

# ── 7. Systemd unit for docker compose ───────────────────────────────────────
install_systemd_unit() {
    local unit="/etc/systemd/system/task-scheduler-mcp.service"
    if [[ -f "$unit" ]]; then
        log "systemd unit already installed, skipping."
        return
    fi
    log "Installing task-scheduler-mcp systemd unit..."
    cat > "$unit" <<EOF
[Unit]
Description=Task Scheduler MCP (docker compose)
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${DEPLOY_DIR}
User=${DEPLOY_USER}
ExecStart=/usr/bin/docker compose --profile full up -d --remove-orphans
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable task-scheduler-mcp.service
    log "task-scheduler-mcp.service enabled (starts on reboot)."
}

# ── 8. Backup cron ────────────────────────────────────────────────────────────
install_backup_cron() {
    local script="/etc/cron.daily/pg-backup.sh"
    if [[ -f "$script" ]] && [[ -x "$script" ]]; then
        log "Backup cron already installed, skipping."
        return
    fi
    log "Installing /etc/cron.daily/pg-backup.sh..."
    cat > "$script" <<'BACKUP'
#!/usr/bin/env bash
# Nightly Postgres → Cloudflare R2 backup with 7-day retention.
set -euo pipefail

DEPLOY_DIR="/opt/task_scheduler_mcp"
TS=$(date -u +%Y%m%d-%H%M%S)
DUMP_FILE="/tmp/scheduler-${TS}.sql.gz"

cd "$DEPLOY_DIR"

# Dump and compress
docker compose exec -T postgres pg_dump -U postgres scheduler \
    | gzip > "$DUMP_FILE"

# Upload to R2
rclone copy "$DUMP_FILE" r2:task-scheduler-mcp-backups/

# Clean local temp file
rm -f "$DUMP_FILE"

# Enforce 7-day retention in R2
rclone delete --min-age 7d r2:task-scheduler-mcp-backups/
BACKUP
    chmod 750 "$script"
    log "Backup cron installed: $script"
}

# ── 9. rclone + R2 config ─────────────────────────────────────────────────────
install_rclone() {
    if command -v rclone &>/dev/null; then
        log "rclone already installed ($(rclone --version | head -1)), skipping."
        return
    fi
    log "Installing rclone..."
    curl -fsSL https://rclone.org/install.sh | bash
}

configure_rclone_r2() {
    local rclone_conf="/root/.config/rclone/rclone.conf"
    if [[ -f "$rclone_conf" ]] && grep -q "\[r2\]" "$rclone_conf" 2>/dev/null; then
        log "rclone R2 remote already configured, skipping."
        return
    fi

    # Read credentials from .env
    if [[ ! -f "$DEPLOY_DIR/.env" ]]; then
        log "WARNING: $DEPLOY_DIR/.env not found; skipping rclone R2 config."
        log "  Run configure_rclone_r2 manually after filling in .env."
        return
    fi

    local account_id access_key secret_key
    account_id=$(grep '^R2_ACCOUNT_ID=' "$DEPLOY_DIR/.env" | cut -d= -f2- | tr -d '"')
    access_key=$(grep '^R2_ACCESS_KEY_ID=' "$DEPLOY_DIR/.env" | cut -d= -f2- | tr -d '"')
    secret_key=$(grep '^R2_SECRET_ACCESS_KEY=' "$DEPLOY_DIR/.env" | cut -d= -f2- | tr -d '"')

    if [[ "$account_id" == "change-me" || -z "$account_id" ]]; then
        log "WARNING: R2 credentials not set in .env; skipping rclone R2 config."
        log "  Fill in R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY, then re-run."
        return
    fi

    mkdir -p "$(dirname "$rclone_conf")"
    cat >> "$rclone_conf" <<EOF

[r2]
type = s3
provider = Cloudflare
access_key_id = ${access_key}
secret_access_key = ${secret_key}
endpoint = https://${account_id}.r2.cloudflarestorage.com
acl = private
EOF
    chmod 600 "$rclone_conf"
    log "rclone R2 remote configured."
}

# ── 10. SSH hardening ─────────────────────────────────────────────────────────
harden_ssh() {
    local sshd_cfg="/etc/ssh/sshd_config"
    local changed=0

    set_sshd_option() {
        local key="$1" value="$2"
        if grep -qE "^\s*${key}\s+${value}" "$sshd_cfg"; then
            return  # already set
        fi
        if grep -qE "^\s*#?\s*${key}\s+" "$sshd_cfg"; then
            sed -i "s|^\s*#\?\s*${key}\s.*|${key} ${value}|" "$sshd_cfg"
        else
            echo "${key} ${value}" >> "$sshd_cfg"
        fi
        changed=1
    }

    log "Hardening SSH..."
    set_sshd_option "PasswordAuthentication" "no"
    set_sshd_option "PermitRootLogin" "prohibit-password"

    if [[ $changed -eq 1 ]]; then
        sshd -t  # validate config before reloading
        # Ubuntu/Debian use ssh.service, Red Hat/Fedora use sshd.service
        systemctl reload ssh 2>/dev/null || systemctl reload sshd
        log "SSH reloaded with hardened config."
    else
        log "SSH already hardened, skipping."
    fi
}

# ── 11. fail2ban ──────────────────────────────────────────────────────────────
configure_fail2ban() {
    local jail="/etc/fail2ban/jail.local"
    if [[ -f "$jail" ]] && grep -q "\[sshd\]" "$jail" 2>/dev/null; then
        log "fail2ban already configured, skipping."
    else
        log "Configuring fail2ban SSH jail..."
        cat > "$jail" <<'EOF'
[DEFAULT]
bantime  = 5m
findtime = 5m
maxretry = 5

[sshd]
enabled = true
port    = ssh
logpath = %(sshd_log)s
backend = %(sshd_backend)s
EOF
    fi
    systemctl enable --now fail2ban
}

# ── 12. ufw ───────────────────────────────────────────────────────────────────
configure_ufw() {
    log "Configuring ufw firewall..."
    ufw --force reset >/dev/null
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable
    log "ufw enabled: allow 22/80/443; deny default incoming."
}

# ── 13. unattended-upgrades ───────────────────────────────────────────────────
configure_unattended_upgrades() {
    cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
    systemctl enable --now unattended-upgrades
    log "unattended-upgrades configured (security-only auto-update)."
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    require_root

    log "=== VPS bootstrap starting ==="

    install_packages
    install_docker
    configure_docker_daemon
    create_deploy_user
    setup_repo
    install_systemd_unit
    install_rclone
    configure_rclone_r2
    install_backup_cron
    harden_ssh
    configure_fail2ban
    configure_ufw
    configure_unattended_upgrades

    log ""
    log "=== Bootstrap complete ==="
    log ""
    log "Next steps:"
    log "  1. Edit $DEPLOY_DIR/.env — fill in POSTGRES_PASSWORD, R2_*, MCP_USER_ID, MCP_USER_TZ"
    log "  2. Re-run this script to configure rclone (needs R2 credentials)"
    log "  3. Start services: cd $DEPLOY_DIR && sudo -u $DEPLOY_USER docker compose up -d"
    log "     OR: systemctl start task-scheduler-mcp"
    log "  4. Verify: sudo -u $DEPLOY_USER docker compose ps"
    log "  5. DNS: point scheduler.paynepew.dev → this VPS IP"
}

main "$@"
