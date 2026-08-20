#!/bin/bash
# Bot management script — safe for use while Claude Code is running.
# Usage:
#   bash bot.sh start         — start the bot (writes PID to .bot.pid)
#   bash bot.sh stop          — stop the bot using saved PID
#   bash bot.sh restart       — stop + start
#   bash bot.sh log           — tail the bot log (live)
#   bash bot.sh status        — check if bot is running
#   bash bot.sh grep <kw>     — filter log by keyword (e.g. "[收到]" "[回复]" open_id)

PIDFILE="bot.pid"
LOGFILE="/tmp/bot.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

start_bot() {
    # Refuse to start if a bare lark-cli subscriber is running (C mode) — they share the bot and would compete for messages.
    local lark_sub_pid
    lark_sub_pid=$(tasklist 2>/dev/null | grep "lark-cli.exe" | awk '{print $2}' | head -1)
    if [ -n "$lark_sub_pid" ]; then
        echo "ERROR: lark-cli.exe is already running (PID $lark_sub_pid)."
        echo "Stop it first to avoid competing for messages, then retry."
        return 1
    fi

    if [ -f "$PIDFILE" ]; then
        local old_pid
        old_pid=$(cat "$PIDFILE")
        if kill -0 "$old_pid" 2>/dev/null; then
            echo "Bot already running (PID $old_pid). Use 'stop' first."
            return 1
        fi
        rm -f "$PIDFILE"
    fi

    cd "$SCRIPT_DIR" || exit 1
    echo "===== Bot started at $(date) =====" >> "$LOGFILE"
    python -m src.main >> "$LOGFILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    echo "Bot started (PID $pid). Log: $LOGFILE"

    # Wait a moment and verify it's still alive
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        head -5 "$LOGFILE"
    else
        echo "ERROR: Bot exited immediately. Check log:"
        cat "$LOGFILE"
        rm -f "$PIDFILE"
        return 1
    fi
}

stop_bot() {
    if [ ! -f "$PIDFILE" ]; then
        echo "No PID file found. Bot may not be running."
        return 1
    fi

    local pid
    pid=$(cat "$PIDFILE")
    echo "Stopping bot (PID $pid)..."

    if kill -0 "$pid" 2>/dev/null; then
        # Kill the python process tree
        taskkill //F //T //PID "$pid" > /dev/null 2>&1

        # Kill any orphaned python bot processes from previous runs
        local py_orphans
        py_orphans=$(powershell -Command "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { \$_.CommandLine -like '*src.main*' } | ForEach-Object { \$_.ProcessId }" 2>/dev/null)
        for opid in $py_orphans; do
            echo "  Cleaning orphaned python bot (PID $opid)"
            taskkill //F //PID "$opid" > /dev/null 2>&1
        done

        # Also kill any orphaned lark-cli processes (but NEVER claude.exe)
        # Give a moment for tree kill to propagate
        sleep 0.5
        local orphans
        orphans=$(tasklist 2>/dev/null | grep "lark-cli.exe" | awk '{print $2}')
        for opid in $orphans; do
            echo "  Cleaning orphaned lark-cli.exe (PID $opid)"
            taskkill //F //PID "$opid" > /dev/null 2>&1
        done

        # Kill orphaned Node.js lark-cli subscriber processes
        # lark-cli.cmd is a wrapper; the real process is node.exe running @larksuite/cli
        local node_orphans
        node_orphans=$(powershell -Command "Get-CimInstance Win32_Process -Filter \"name='node.exe'\" | Where-Object { \$_.CommandLine -like '*@larksuite*cli*event*subscribe*' } | ForEach-Object { \$_.ProcessId }" 2>/dev/null)
        for opid in $node_orphans; do
            echo "  Cleaning orphaned node lark-cli subscriber (PID $opid)"
            taskkill //F //PID "$opid" > /dev/null 2>&1
        done
        echo "Bot stopped."
    else
        echo "Process $pid not found (already exited)."
    fi
    rm -f "$PIDFILE"

    # Remove stale subscriber lock files to prevent reconnect failures
    sleep 0.5
    local lark_home="${LARK_BOT_HOME:-$HOME}"
    for lock in "$lark_home"/.lark-cli/locks/subscribe_*.lock; do
        [ -f "$lock" ] && rm -f "$lock" 2>/dev/null && echo "  Removed stale lock: $lock"
    done
}

case "${1:-}" in
    start)
        start_bot
        ;;
    stop)
        stop_bot
        ;;
    restart)
        stop_bot
        start_bot
        ;;
    log)
        tail -f "$LOGFILE"
        ;;
    grep)
        shift
        grep --color=auto "$@" "$LOGFILE"
        ;;
    status)
        if [ -f "$PIDFILE" ]; then
            pid=$(cat "$PIDFILE")
            if kill -0 "$pid" 2>/dev/null; then
                echo "Bot is running (PID $pid)"
            else
                echo "Bot PID $pid is stale (not running)"
                rm -f "$PIDFILE"
            fi
        else
            echo "Bot is not running (no PID file)"
        fi
        ;;
    *)
        echo "Usage: bash bot.sh {start|stop|restart|log|status|grep <kw>}"
        exit 1
        ;;
esac
