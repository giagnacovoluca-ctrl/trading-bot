#!/usr/bin/env bash
# Wrapper con auto-restart per injective_autopilot/main.py.
# Se il processo termina (crash, watchdog os._exit, ecc.) viene rilanciato
# dopo una breve pausa. Ctrl+C ripetuto (gestito da main.py) esce normalmente;
# per fermare anche il loop, premere Ctrl+C una seconda volta entro 1s o
# usare Ctrl+C e poi attendere: lo script esce se main.py esce con status 0
# entro la seconda finestra... in pratica: per stop definitivo, kill questo script.

cd "$(dirname "$0")"
VENV_PY="$(cd .. && pwd)/venv/bin/python"

# Fix gRPC SSL crash: "pem_root_certs != nullptr" su Linux senza certs configurati
export GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/etc/ssl/certs/ca-certificates.crt

# ── funding_farmer: processo background con auto-restart ogni 30s ──────────────
_farmer_loop() {
    local child_pid=""
    _stop_farmer() { [ -n "$child_pid" ] && kill "$child_pid" 2>/dev/null; exit 0; }
    trap '_stop_farmer' INT TERM
    while true; do
        echo "[run_loop] $(date '+%Y-%m-%d %H:%M:%S') avvio funding_farmer.py"
        "$VENV_PY" funding_farmer.py &
        child_pid=$!
        wait "$child_pid"
        local ec=$?
        [ $ec -eq 0 ] || [ $ec -eq 130 ] || [ $ec -eq 143 ] && break  # uscita pulita
        echo "[run_loop] $(date '+%Y-%m-%d %H:%M:%S') funding_farmer terminato (exit=$ec) — riavvio in 30s"
        sleep 30
    done
}
_farmer_loop &
FARMER_PID=$!
echo "[run_loop] funding_farmer avviato in background (PID $FARMER_PID)"

# Propaga Ctrl+C/SIGTERM anche al farmer e ferma il loop main
_stop_all() { kill "$FARMER_PID" 2>/dev/null; exit 0; }
trap '_stop_all' TERM

# ── main.py: loop con auto-restart, exit pulito ferma tutto ───────────────────
while true; do
    echo "[run_loop] $(date '+%Y-%m-%d %H:%M:%S') avvio main.py ($@)"
    "$VENV_PY" main.py "$@"
    code=$?
    echo "[run_loop] $(date '+%Y-%m-%d %H:%M:%S') main.py terminato (exit=$code)"
    if [ $code -eq 0 ] || [ $code -eq 130 ] || [ $code -eq 143 ]; then
        echo "[run_loop] uscita intenzionale (exit=$code) — stop loop"
        break
    fi
    echo "[run_loop] riavvio in 5s..."
    sleep 5
done

# Ferma il funding_farmer
kill "$FARMER_PID" 2>/dev/null
echo "[run_loop] funding_farmer fermato (PID $FARMER_PID)"
