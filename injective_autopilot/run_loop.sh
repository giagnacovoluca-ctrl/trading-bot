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
    while true; do
        echo "[run_loop] $(date '+%Y-%m-%d %H:%M:%S') avvio funding_farmer.py"
        "$VENV_PY" funding_farmer.py
        echo "[run_loop] $(date '+%Y-%m-%d %H:%M:%S') funding_farmer terminato (exit=$?) — riavvio in 30s"
        sleep 30
    done
}
_farmer_loop &
FARMER_PID=$!
echo "[run_loop] funding_farmer avviato in background (PID $FARMER_PID)"

# ── main.py: loop con auto-restart, exit pulito ferma tutto ───────────────────
while true; do
    echo "[run_loop] $(date '+%Y-%m-%d %H:%M:%S') avvio main.py ($@)"
    "$VENV_PY" main.py "$@"
    code=$?
    echo "[run_loop] $(date '+%Y-%m-%d %H:%M:%S') main.py terminato (exit=$code)"
    if [ $code -eq 0 ]; then
        echo "[run_loop] exit pulito (0) — stop loop"
        break
    fi
    echo "[run_loop] riavvio in 5s..."
    sleep 5
done

# Ferma il funding_farmer quando main.py esce con codice 0
kill "$FARMER_PID" 2>/dev/null
echo "[run_loop] funding_farmer fermato (PID $FARMER_PID)"
