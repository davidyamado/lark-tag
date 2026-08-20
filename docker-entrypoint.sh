#!/bin/sh
set -e

echo "[entrypoint] running as: $(id)"
echo "[entrypoint] /var/lark-bot permissions: $(ls -ld /var/lark-bot 2>&1)"

# Security: this bot never calls the Kubernetes API. Remove the auto-mounted
# ServiceAccount token and clear the env vars that point at the API server, so
# even if a malicious user message escapes the bubblewrap sandbox, the K8s API
# isn't reachable from inside the container.
rm -rf /var/run/secrets/kubernetes.io 2>/dev/null || true
unset KUBERNETES_SERVICE_HOST KUBERNETES_SERVICE_PORT \
      KUBERNETES_PORT KUBERNETES_PORT_443_TCP \
      KUBERNETES_PORT_443_TCP_ADDR KUBERNETES_PORT_443_TCP_PORT \
      KUBERNETES_PORT_443_TCP_PROTO KUBERNETES_SERVICE_PORT_HTTPS

LARK_CONFIG_DIR="${LARK_BOT_HOME:-/var/lark-bot/config}/.lark-cli"
mkdir -p "$LARK_CONFIG_DIR"

# Ensure Claude Code config dir exists under the main PVC mount.
# Using /var/lark-bot/claude-config instead of a subPath mount avoids
# the NAS subPath permission issue (subPaths are created root:root by k8s).
CLAUDE_DIR="${CLAUDE_HOME:-/var/lark-bot/claude-config}"
mkdir -p "$CLAUDE_DIR"
echo "[entrypoint] claude-config permissions: $(ls -ld "$CLAUDE_DIR" 2>&1)"

# Prefer config.json from env vars; fall back to copying the ConfigMap mount.
# The ConfigMap mount (/app/.lark-cli/config.json) is read-only, so we always
# work from a writable copy under LARK_BOT_HOME.
if [ -n "${FEISHU_APP_ID:-}" ] && [ -n "${FEISHU_APP_SECRET:-}" ]; then
    python3 -c "
import json, sys
config = {'apps': [{'appId': sys.argv[1], 'appSecret': sys.argv[2], 'brand': 'feishu', 'lang': 'zh', 'users': []}]}
with open(sys.argv[3], 'w') as f:
    json.dump(config, f)
" "${FEISHU_APP_ID}" "${FEISHU_APP_SECRET}" "${LARK_CONFIG_DIR}/config.json"
    echo "Written lark-cli bot config from env vars: ${LARK_CONFIG_DIR}/config.json"
elif [ -f "/app/.lark-cli/config.json" ]; then
    cp "/app/.lark-cli/config.json" "${LARK_CONFIG_DIR}/config.json"
    echo "Copied lark-cli bot config from ConfigMap: ${LARK_CONFIG_DIR}/config.json"
fi

# Install meegle skill for Claude Code if not already present.
# Skills live in $CLAUDE_DIR/skills/meegle/ — Claude Code auto-loads them.
MEEGLE_SKILL_DIR="${CLAUDE_DIR}/skills/meegle"
MEEGLE_SKILL_INSTALLED=0
if [ ! -f "${MEEGLE_SKILL_DIR}/SKILL.md" ]; then
    echo "[entrypoint] Installing meegle skill into Claude Code config..."
    mkdir -p "${MEEGLE_SKILL_DIR}"
    cp -r /opt/meegle-skill/. "${MEEGLE_SKILL_DIR}/"
    MEEGLE_SKILL_INSTALLED=1
    echo "[entrypoint] meegle skill installed: ${MEEGLE_SKILL_DIR}"
fi

# Install agent-browser skill for Claude Code if not already present.
AGENT_BROWSER_SKILL_DIR="${CLAUDE_DIR}/skills/agent-browser"
AGENT_BROWSER_SKILL_INSTALLED=0
if [ ! -f "${AGENT_BROWSER_SKILL_DIR}/SKILL.md" ]; then
    echo "[entrypoint] Installing agent-browser skill into Claude Code config..."
    mkdir -p "${AGENT_BROWSER_SKILL_DIR}"
    cp -r /opt/agent-browser-skill/. "${AGENT_BROWSER_SKILL_DIR}/"
    AGENT_BROWSER_SKILL_INSTALLED=1
    echo "[entrypoint] agent-browser skill installed: ${AGENT_BROWSER_SKILL_DIR}"
fi

# Install access-checked S3 reader skill for Claude Code if not already present.
S3_ACCESS_READER_SKILL_DIR="${CLAUDE_DIR}/skills/s3-access-reader"
S3_ACCESS_READER_SKILL_INSTALLED=0
if [ ! -f "${S3_ACCESS_READER_SKILL_DIR}/SKILL.md" ]; then
    echo "[entrypoint] Installing s3-access-reader skill into Claude Code config..."
    mkdir -p "${S3_ACCESS_READER_SKILL_DIR}"
    cp -r /opt/s3-access-reader-skill/. "${S3_ACCESS_READER_SKILL_DIR}/"
    S3_ACCESS_READER_SKILL_INSTALLED=1
    echo "[entrypoint] s3-access-reader skill installed: ${S3_ACCESS_READER_SKILL_DIR}"
fi

# Fix ownership of NAS-mounted volumes.
# NFS doesn't respect k8s fsGroup, so we chown explicitly here as root
# before dropping privileges. Errors are ignored in case dirs don't exist yet.
#
# IMPORTANT: Do NOT use -R on /var/lark-bot directly. It contains a large tree
# (claude-config/skills, user session data) already owned by botuser from prior
# runs — recursing through it on NFS is slow. Only chown what this entrypoint
# created or modified.
chown botuser:botuser /var/lark-bot /var/lark-bot/config /var/lark-bot/users \
    "$CLAUDE_DIR" /home/botuser/.claude \
    /var/lark-bot/.claude.json 2>/dev/null || true
# Chown the lark-cli config dir (config.json written here as root above)
chown -R botuser:botuser "$LARK_CONFIG_DIR" 2>/dev/null || true
# Only chown skills if just installed this run (cp runs as root)
if [ "$MEEGLE_SKILL_INSTALLED" = "1" ]; then
    chown -R botuser:botuser "${MEEGLE_SKILL_DIR}" 2>/dev/null || true
fi
if [ "$AGENT_BROWSER_SKILL_INSTALLED" = "1" ]; then
    chown -R botuser:botuser "${AGENT_BROWSER_SKILL_DIR}" 2>/dev/null || true
fi
if [ "$S3_ACCESS_READER_SKILL_INSTALLED" = "1" ]; then
    chown -R botuser:botuser "${S3_ACCESS_READER_SKILL_DIR}" 2>/dev/null || true
fi

# Point lark-cli at the writable copy so lock files can be created.
# This overrides any pod-level LARKSUITE_CLI_CONFIG_DIR set by ops.
export LARKSUITE_CLI_CONFIG_DIR="$LARK_CONFIG_DIR"

# Drop from root to botuser — Claude Code refuses --dangerously-skip-permissions as root
exec gosu botuser python -m src.main
