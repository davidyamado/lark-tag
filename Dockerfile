FROM python:3.11-slim

# Node.js 20 LTS — required for lark-cli and Claude Code CLI
# chromium — headless browser for agent-browser (web automation tool)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg gosu chromium bubblewrap \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# CLI tools
RUN npm install -g @larksuite/cli@1.0.54 @anthropic-ai/claude-code @lark-project/meegle agent-browser

# Bake meegle skill files into the image so entrypoint can install them
# without needing internet access at container start time.
# The npm package does not ship the skills/ directory, so fetch from GitHub.
RUN mkdir -p /opt/meegle-skill && \
    curl -fsSL https://github.com/larksuite/meegle-cli/archive/refs/heads/main.tar.gz \
        | tar -xz -C /tmp --strip-components=2 \
              meegle-cli-main/skills/meegle \
    && cp -r /tmp/meegle/. /opt/meegle-skill/ \
    && rm -rf /tmp/meegle

# Bake agent-browser skill (web automation via Chromium for AI agents).
RUN mkdir -p /opt/agent-browser-skill /tmp/ab-skill && \
    curl -fsSL https://github.com/vercel-labs/agent-browser/archive/refs/heads/main.tar.gz \
        | tar -xz -C /tmp/ab-skill --strip-components=3 \
              agent-browser-main/skills/agent-browser \
    && cp -r /tmp/ab-skill/. /opt/agent-browser-skill/ \
    && rm -rf /tmp/ab-skill

WORKDIR /app

# Python runtime dependencies only
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source and entrypoint
COPY src/ ./src/
COPY scripts/audit_query.py ./scripts/audit_query.py
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# Bake the access-checked S3 reader skill into the image. The entrypoint copies
# this into the shared Claude Code skills directory on startup.
COPY docs/s3-access-reader/s3-access-reader/ /opt/s3-access-reader-skill/

# Install the PreToolUse Bash guard into /opt (which is bind-mounted read-only
# into the bubblewrap sandbox, while /app is not). The hook script must live
# at a path the sandboxed Claude subprocess can actually reach.
COPY scripts/sandbox-lark-cli /opt/bot-guard/sandbox-lark-cli
RUN mkdir -p /opt/bot-guard \
    && cp /app/src/bash_guard.py /opt/bot-guard/bash_guard.py \
    && chmod 755 /opt/bot-guard/bash_guard.py /opt/bot-guard/sandbox-lark-cli \
    && ln -sfn "$(readlink -f /usr/bin/lark-cli)" /opt/bot-guard/real-lark-cli

# Create non-root user — Claude Code CLI refuses --dangerously-skip-permissions as root
# Explicit UID/GID 1001 so k8s securityContext.fsGroup can reference a known value
RUN groupadd -g 1001 botuser && useradd -u 1001 -g 1001 -m -d /home/botuser botuser

# Create default runtime directories and hand ownership to botuser
# Ops team mounts persistent volumes here:
#   /home/botuser/.claude        — Claude Code config (settings, plugins, skills)
#   /var/lark-bot/config         — Bot lark-cli credentials (LARK_BOT_HOME)
#   /var/lark-bot/users          — Per-user lark-cli/meegle credentials (LARK_USERS_DIR)
#   PostgreSQL via POSTGRES_URL  — user/session/job state
# Meegle credentials (~/.meegle/config.json) are stored per-user under LARK_USERS_DIR
# via device-code OAuth (no ops setup required — users authorize on first use).
RUN mkdir -p /var/lark-bot/config /var/lark-bot/users /home/botuser/.claude \
    && chown -R botuser:botuser /var/lark-bot /home/botuser

# Harden /app LAST so neither the create-user nor the chown-runtime-dirs steps
# above can wipe out this ownership: source code owned by root, readable only
# by the botuser group.
# - botuser keeps rx via group (so `python -m src.main` still works)
# - non-root, non-botuser identities inside the container get no access
# - In production the bubblewrap sandbox doesn't mount /app at all, so the
#   Claude subprocess can't see source anyway; this is defense in depth for
#   the BOT_SANDBOX=0 case and to prevent accidental writes by the bot itself.
# PYTHONDONTWRITEBYTECODE=1 below disables __pycache__ writes (the source
# tree is read-only for botuser after the chmod).
RUN chown -R root:botuser /app && chmod -R g-w,o-rwx /app && chmod 750 /app

# Entrypoint runs as root to fix NAS volume ownership, then drops to botuser via gosu.
# Do NOT set USER botuser here — the privilege drop happens inside docker-entrypoint.sh.

# Environment variable defaults (override at runtime as needed)
ENV LARK_BOT_HOME=/var/lark-bot/config \
    LARK_USERS_DIR=/var/lark-bot/users \
    CLAUDE_HOME=/var/lark-bot/claude-config \
    HOME=/home/botuser \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["./docker-entrypoint.sh"]
