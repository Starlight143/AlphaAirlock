#!/usr/bin/env bash
# Cross-platform launcher (macOS / Linux) for the Agentic Alpha System.
# Boots FastAPI backend + Next.js frontend, traps Ctrl+C / exit so neither
# child is orphaned. Mirrors start.ps1 for Windows users.
#
# Port handling: BACKEND_PORT / FRONTEND_PORT are PREFERRED ports. If a
# preferred port is already in use (or reserved by the OS), the launcher
# auto-selects the next free port and rewires NEXT_PUBLIC_API_BASE and
# ALPHA_ALLOWED_ORIGINS so the frontend and CORS keep matching the backend.
# Set PORT_AUTO_SELECT=0 to disable auto-select and fail fast on a busy port.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PY="${PYTHON:-python3}"

BACKEND_PID=""
FRONTEND_PID=""
CAFFEINATE_PID=""

cleanup() {
    echo ""
    echo "[start.sh] Shutting down Agentic Alpha System..."
    for pid in "$BACKEND_PID" "$FRONTEND_PID" "$CAFFEINATE_PID"; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            # Kill the whole process group (negative PID) so uvicorn's
            # reloader children and Next's workers die too. (caffeinate is a
            # lone process, so only the fallback single-PID kill applies to it.)
            kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
        fi
    done
    wait 2>/dev/null || true
    echo "[start.sh] Shutdown complete."
}
trap cleanup EXIT INT TERM

# --- macOS: keep the system awake for the launcher's whole lifetime ----------
# Symptom this fixes: once the Mac's screen locks it idle-sleeps the whole
# machine; sleep FREEZES the uvicorn process and tears down every TCP socket,
# so on wake all pooled OpenRouter connections are dead and agent calls fail
# en masse ("can't reach OpenRouter after the screen locks").
#
# `caffeinate -i -m -s -w "$$"` asserts no idle-system-sleep / no disk-sleep /
# no system-sleep-on-AC, scoped to THIS script's PID ($$): the instant the
# launcher exits (Ctrl+C included) the assertion auto-releases. No sudo, no
# permanent `pmset` change, and the display is deliberately left to sleep
# normally (no -d) — only the system/disk are held awake, which is all that
# keeps the network stack and the backend alive while you're away.
if [[ "$(uname -s)" == "Darwin" ]] && command -v caffeinate >/dev/null 2>&1; then
    caffeinate -i -m -s -w "$$" &
    CAFFEINATE_PID=$!
    echo "[start.sh] caffeinate active (pid $CAFFEINATE_PID): system idle-sleep disabled while the app runs."
fi

# --- helpers ---------------------------------------------------------------

# Pick a free TCP port via Python (reliable + cross-platform).
# Args: <host> <preferred> <auto:0/1/...> <exclude-csv>
# Prints the chosen port to stdout; exits non-zero if none could be found
# (e.g. auto-select disabled and the preferred port is busy).
find_free_port() {
    "$PY" - "$@" <<'PY'
import socket, sys

host = sys.argv[1]
pref = int(sys.argv[2])
auto = sys.argv[3].strip().lower()
exclude = set(int(x) for x in sys.argv[4].split(',') if x.strip())

# Probe the same interface the real service binds: 0.0.0.0/''/* -> all
# interfaces (''), localhost -> 127.0.0.1, otherwise the given host.
if host in ('', '0.0.0.0', '*', '::', '[::]'):
    probe = ''
elif host == 'localhost':
    probe = '127.0.0.1'
else:
    probe = host

def free(p):
    if p in exclude:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((probe, p))
        return True
    except OSError:
        return False
    finally:
        s.close()

if free(pref):
    print(pref); sys.exit(0)

if auto in ('0', 'false', 'no', 'off'):
    sys.exit(2)  # auto-select disabled -> fail fast

for p in range(pref + 1, min(pref + 513, 65536)):
    if free(p):
        print(p); sys.exit(0)

# Guaranteed fallback: OS-assigned ephemeral port (never a reserved one).
for _ in range(16):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((probe, 0))
        p = s.getsockname()[1]
    finally:
        s.close()
    if p not in exclude:
        print(p); sys.exit(0)

sys.exit(3)
PY
}

# Map a bind host to something a local browser / the frontend can dial.
browser_host() {
    case "$1" in
        ""|0.0.0.0|"*"|"::"|"[::]") echo "127.0.0.1" ;;
        *) echo "$1" ;;
    esac
}

# Self-healing dependency install (see start.ps1 for the lxml RECORD-file story).
install_deps() {
    echo "[start.sh] Step 1/4: Installing Python dependencies"
    if [[ -n "${SKIP_PIP_INSTALL:-}" ]]; then
        echo "[start.sh] SKIP_PIP_INSTALL set - skipping pip install"
        return 0
    fi
    if "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt; then
        return 0
    fi
    echo "[start.sh] pip install failed. Attempting known-issue repair (lxml RECORD file)..."
    "$PY" -m pip install --disable-pip-version-check --force-reinstall --no-deps "lxml>=5.2,<6" || true
    if "$PY" -m pip install --disable-pip-version-check -r requirements.txt; then
        echo "[start.sh] Dependency repair succeeded."
        return 0
    fi
    echo "[start.sh] WARNING: pip install still failing after repair."
    echo "[start.sh] Continuing: the only affected feature is full-text article"
    echo "[start.sh] enrichment (trafilatura/lxml), lazy-imported and OFF by default."
    return 0
}

# --- Step 1: dependencies --------------------------------------------------
install_deps

# --- Step 2: synthetic dataset ---------------------------------------------
echo "[start.sh] Step 2/4: Ensuring synthetic_btc.csv exists"
DATA_CSV="$ROOT_DIR/backend/data/synthetic_btc.csv"
if [[ ! -f "$DATA_CSV" ]]; then
    "$PY" backend/core/data_gen.py
fi

# Provider-aware key warning. Defaults to 'anthropic' when LLM_PROVIDER is unset.
PROVIDER_RAW="${LLM_PROVIDER:-anthropic}"
PROVIDER_NORM="$(printf '%s' "$PROVIDER_RAW" | tr '[:upper:]' '[:lower:]' | xargs)"
case "$PROVIDER_NORM" in
    openrouter|openai|openai-compatible|openai_compatible)
        if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
            echo "[start.sh] WARNING: LLM_PROVIDER=$PROVIDER_RAW but OPENROUTER_API_KEY is not set - agent calls will fail."
        fi
        ;;
    auto)
        if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${OPENROUTER_API_KEY:-}" ]]; then
            echo "[start.sh] WARNING: LLM_PROVIDER=auto but neither ANTHROPIC_API_KEY nor OPENROUTER_API_KEY is set - agent calls will fail."
        fi
        ;;
    *)
        if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
            echo "[start.sh] WARNING: LLM_PROVIDER=$PROVIDER_RAW but ANTHROPIC_API_KEY is not set - agent calls will fail."
        fi
        ;;
esac

# --- Port selection (preferred -> auto-select free) ------------------------
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
FRONTEND_HOST="${FRONTEND_HOST:-0.0.0.0}"
BACKEND_PORT_PREF="${BACKEND_PORT:-8000}"
FRONTEND_PORT_PREF="${FRONTEND_PORT:-3000}"
AUTO_SELECT="${PORT_AUTO_SELECT:-1}"

if ! BACKEND_PORT_SEL="$(find_free_port "$BACKEND_HOST" "$BACKEND_PORT_PREF" "$AUTO_SELECT" "")"; then
    echo "[start.sh] ERROR: backend port $BACKEND_PORT_PREF is in use/reserved and PORT_AUTO_SELECT=$AUTO_SELECT."
    exit 1
fi
if ! FRONTEND_PORT_SEL="$(find_free_port "$FRONTEND_HOST" "$FRONTEND_PORT_PREF" "$AUTO_SELECT" "$BACKEND_PORT_SEL")"; then
    echo "[start.sh] ERROR: frontend port $FRONTEND_PORT_PREF is in use/reserved and PORT_AUTO_SELECT=$AUTO_SELECT."
    exit 1
fi

if [[ "$BACKEND_PORT_SEL" != "$BACKEND_PORT_PREF" ]]; then
    echo "[start.sh] Backend port $BACKEND_PORT_PREF unavailable - using $BACKEND_PORT_SEL instead."
fi
if [[ "$FRONTEND_PORT_SEL" != "$FRONTEND_PORT_PREF" ]]; then
    echo "[start.sh] Frontend port $FRONTEND_PORT_PREF unavailable - using $FRONTEND_PORT_SEL instead."
fi

BACKEND_BROWSER_HOST="$(browser_host "$BACKEND_HOST")"
FRONTEND_BROWSER_HOST="$(browser_host "$FRONTEND_HOST")"

# Wire frontend -> backend URL + backend CORS allowlist to the chosen ports.
export NEXT_PUBLIC_API_BASE="http://${BACKEND_BROWSER_HOST}:${BACKEND_PORT_SEL}"
export BACKEND_HOST
export BACKEND_PORT="$BACKEND_PORT_SEL"

ORIGINS="http://localhost:${FRONTEND_PORT_SEL},http://127.0.0.1:${FRONTEND_PORT_SEL}"
if [[ "$FRONTEND_BROWSER_HOST" != "localhost" && "$FRONTEND_BROWSER_HOST" != "127.0.0.1" ]]; then
    ORIGINS="${ORIGINS},http://${FRONTEND_BROWSER_HOST}:${FRONTEND_PORT_SEL}"
fi
if [[ -n "${ALPHA_ALLOWED_ORIGINS:-}" ]]; then
    ORIGINS="${ALPHA_ALLOWED_ORIGINS},${ORIGINS}"
fi
export ALPHA_ALLOWED_ORIGINS="$ORIGINS"

# --- Step 3: backend -------------------------------------------------------
echo "[start.sh] Step 3/4: Launching FastAPI backend (http://${BACKEND_BROWSER_HOST}:${BACKEND_PORT_SEL})"
"$PY" -m uvicorn backend.app.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT_SEL" &
BACKEND_PID=$!

# --- Step 4: frontend ------------------------------------------------------
echo "[start.sh] Step 4/4: Launching Next.js frontend (http://${FRONTEND_BROWSER_HOST}:${FRONTEND_PORT_SEL})"
cd frontend
if [[ ! -d node_modules ]]; then
    echo "[start.sh] node_modules missing - running 'npm install' (one-time)"
    npm install --no-audit --no-fund
fi
# Explicit '-p' (last one wins over package.json's baked port). PORT is a
# belt-and-braces fallback.
PORT="$FRONTEND_PORT_SEL" npm run dev -- -p "$FRONTEND_PORT_SEL" -H "$FRONTEND_HOST" &
FRONTEND_PID=$!
cd "$ROOT_DIR"

echo "[start.sh] All services up. Press Ctrl+C to stop both."
echo "[start.sh] Backend : http://${BACKEND_BROWSER_HOST}:${BACKEND_PORT_SEL} (docs: /docs)"
echo "[start.sh] Frontend: http://${FRONTEND_BROWSER_HOST}:${FRONTEND_PORT_SEL}"
echo "[start.sh] API base: $NEXT_PUBLIC_API_BASE  CORS: $ALPHA_ALLOWED_ORIGINS"

# Wait on either child; if one dies, drop to cleanup.
wait -n 2>/dev/null || wait
echo "[start.sh] Backend or frontend exited; cleaning up."
