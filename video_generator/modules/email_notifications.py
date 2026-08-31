"""Notifiche email per i job automatici del generatore video."""

from __future__ import annotations

import os
import smtplib
import socket
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_SHARED_ENV = Path("/home/ubuntu/conscia-mente/.env")


def _load_email_settings() -> tuple[str, str, str, str, int] | None:
    # Le credenziali sono già gestite dal sito Conscia-Mente. Non vengono
    # duplicate nel repository del generatore video.
    load_dotenv(override=False)
    shared_env = Path(os.getenv("EMAIL_ENV_FILE", str(DEFAULT_SHARED_ENV)))
    if shared_env.is_file():
        load_dotenv(shared_env, override=False)

    user = os.getenv("GMAIL_USER", "").strip()
    password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    recipient = (os.getenv("NOTIFICATION_EMAIL") or user).strip()
    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    try:
        port = int(os.getenv("SMTP_PORT", "465"))
    except ValueError:
        port = 465

    if not user or not password or not recipient:
        return None
    return user, password, recipient, host, port


def notify_email(message: str) -> bool:
    """Invia una notifica; un problema SMTP non deve interrompere il cron."""
    settings = _load_email_settings()
    if settings is None:
        print("Email notify skipped: configurazione GMAIL/NOTIFICATION_EMAIL mancante")
        return False

    user, password, recipient, host, port = settings
    clean_message = str(message).strip()
    status = "ERRORE" if "errore" in clean_message.casefold() else "AGGIORNAMENTO"

    mail = EmailMessage()
    mail["From"] = f"Conscia-Mente Automazioni <{user}>"
    mail["To"] = recipient
    mail["Subject"] = f"[{status}] Generatore video Conscia-Mente"
    mail.set_content(
        f"Notifica automatica dal server {socket.gethostname()}:\n\n{clean_message}\n"
    )

    try:
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            smtp.login(user, password)
            smtp.send_message(mail)
        print("Notifica email inviata")
        return True
    except Exception as exc:
        print(f"Errore notifica email: {exc}")
        return False
