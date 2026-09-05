#!/usr/bin/env bash
set -uo pipefail

MODE="${1:-}"
MAX_ATTEMPTS="${VIDEO_QUALITY_MAX_ATTEMPTS:-5}"

case "$MODE" in
  virale|promo|bastian) ;;
  *) echo "Modalità non valida: $MODE" >&2; exit 2 ;;
esac

if ! [[ "$MAX_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || (( MAX_ATTEMPTS > 8 )); then
  echo "VIDEO_QUALITY_MAX_ATTEMPTS deve essere compreso tra 1 e 8" >&2
  exit 2
fi

for (( attempt=1; attempt<=MAX_ATTEMPTS; attempt++ )); do
  echo "Tentativo editoriale $attempt/$MAX_ATTEMPTS (modalità: $MODE)"
  venv_video/bin/python agente_tiktok.py --mode "$MODE" --no-site
  exit_code=$?

  if (( exit_code == 0 )); then
    echo "Pubblicazione completata al tentativo $attempt/$MAX_ATTEMPTS"
    exit 0
  fi
  if (( exit_code != 74 && exit_code != 75 )); then
    echo "Errore tecnico non recuperabile (codice $exit_code)" >&2
    exit "$exit_code"
  fi

  if (( exit_code == 74 )); then
    echo "Valutatore temporaneamente non disponibile; riprovo con un nuovo argomento."
  else
    echo "Contenuto insufficiente scartato; seleziono un nuovo argomento."
  fi
done

venv_video/bin/python -c "from modules.email_notifications import notify_email; notify_email('ATTENZIONE: nessun copione ha superato 7/10 dopo ${MAX_ATTEMPTS} argomenti. Il job sarà riprovato dal prossimo cron.')"
echo "Nessun contenuto debole pubblicato; tentativi esauriti, job rinviato." >&2
exit 0
