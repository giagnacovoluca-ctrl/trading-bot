"""
Cookie Uploader Server — FastAPI version
Serve una pagina web per caricare i cookies TikTok in formato JSON (Cookie-Editor)
e li converte automaticamente nel formato Netscape cookies.txt usato da tiktok-uploader.

Avvio:  python cookie_uploader.py
        oppure: uvicorn cookie_uploader:app --host 0.0.0.0 --port 8888

Accesso dal telefono: http://<IP_VPS>:8888
"""

from __future__ import annotations
import os
import json
import hmac
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, File, UploadFile, Request
from fastapi.responses import HTMLResponse
import uvicorn

# ── Configurazione ────────────────────────────────────────────────────────────
PORT = 8888
COOKIES_FILE = Path(__file__).parent / "cookies.txt"
BACKUP_DIR   = Path(__file__).parent / "temp" / "cookies_backup"

# Password di accesso.
# Cambia qui OPPURE imposta la variabile: COOKIE_UPLOAD_PASSWORD=xxx
UPLOAD_PASSWORD = os.getenv("COOKIE_UPLOAD_PASSWORD", "tiktok2024")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cookie_uploader")

# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(title="Cookie Uploader", docs_url=None, redoc_url=None)


# ── Conversione JSON → Netscape ───────────────────────────────────────────────
def json_to_netscape(cookies: list[dict]) -> tuple[str, int, int]:
    """
    Converte una lista di cookies nel formato Cookie-Editor (JSON)
    nel formato Netscape cookies.txt usato da tiktok-uploader/yt-dlp.

    Formato Netscape (tab-separated):
    [#HttpOnly_]<domain>  <include_subdomains>  <path>  <secure>  <expiry>  <name>  <value>
    """
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generato automaticamente da cookie_uploader.py",
        f"# Data: {datetime.now().isoformat()}",
        "",
    ]

    converted = 0
    skipped   = 0

    for cookie in cookies:
        try:
            name      = cookie.get("name", "")
            value     = cookie.get("value", "")
            domain    = cookie.get("domain", "")
            path      = cookie.get("path", "/")
            secure    = cookie.get("secure", False)
            http_only = cookie.get("httpOnly", False)

            # expiry: Cookie-Editor usa "expirationDate", altri usano "expires"
            expiry = cookie.get("expirationDate") or cookie.get("expires") or 0
            if isinstance(expiry, float):
                expiry = int(expiry)

            # Salta cookies senza nome o valore
            if not name or value is None:
                skipped += 1
                continue

            # Normalizza il dominio
            domain = domain.replace("https://", "").replace("http://", "")
            if domain and not domain.startswith("."):
                domain = f".{domain}"

            include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
            secure_str = "TRUE" if secure else "FALSE"
            prefix = "#HttpOnly_" if http_only else ""

            line = f"{prefix}{domain}\t{include_subdomains}\t{path}\t{secure_str}\t{expiry}\t{name}\t{value}"
            lines.append(line)
            converted += 1

        except Exception as e:
            log.warning(f"Cookie saltato per errore: {e} — {cookie.get('name', '?')}")
            skipped += 1

    log.info(f"Conversione: {converted} convertiti, {skipped} saltati")
    return "\n".join(lines) + "\n", converted, skipped


# ── HTML ──────────────────────────────────────────────────────────────────────
def render_page(alert: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🍪 Cookie Uploader — TikTok</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            min-height: 100vh;
            background: #0a0a0f;
            display: flex;
            align-items: center;
            justify-content: center;
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            padding: 20px;
        }}
        .card {{
            background: linear-gradient(135deg, #12121c 0%, #1a1a2e 100%);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 24px;
            padding: 40px;
            width: 100%;
            max-width: 520px;
            box-shadow:
                0 0 0 1px rgba(255,255,255,0.04),
                0 40px 80px rgba(0,0,0,0.6),
                0 0 60px rgba(255,0,80,0.05);
        }}
        .logo {{ text-align: center; margin-bottom: 32px; }}
        .logo .icon {{
            font-size: 48px; display: block; margin-bottom: 12px;
            animation: pulse 2s ease-in-out infinite;
        }}
        @keyframes pulse {{ 0%,100% {{ transform: scale(1); }} 50% {{ transform: scale(1.08); }} }}
        .logo h1 {{ font-size: 22px; font-weight: 700; color: #fff; letter-spacing: -0.3px; }}
        .logo p {{ font-size: 13px; color: rgba(255,255,255,0.45); margin-top: 6px; }}
        .badge {{
            display: inline-flex; align-items: center; gap: 6px;
            background: rgba(255,0,80,0.12); border: 1px solid rgba(255,0,80,0.25);
            color: #ff4d6d; font-size: 11px; font-weight: 600;
            padding: 4px 10px; border-radius: 20px; letter-spacing: 0.5px; margin-top: 10px;
        }}
        label {{
            display: block; font-size: 12px; font-weight: 600;
            color: rgba(255,255,255,0.5); text-transform: uppercase;
            letter-spacing: 0.8px; margin-bottom: 8px;
        }}
        .field {{ margin-bottom: 20px; }}
        input[type="password"], textarea {{
            width: 100%;
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px; color: #fff; font-size: 14px;
            padding: 14px 16px; outline: none;
            transition: border-color 0.2s, background 0.2s;
            font-family: inherit; resize: vertical;
        }}
        input[type="password"]:focus, textarea:focus {{
            border-color: rgba(255,0,80,0.5);
            background: rgba(255,255,255,0.07);
        }}
        textarea {{
            min-height: 160px;
            font-family: 'SF Mono', 'Fira Code', monospace;
            font-size: 12px; line-height: 1.5;
        }}
        .drop-zone {{
            border: 2px dashed rgba(255,255,255,0.12); border-radius: 12px;
            padding: 24px; text-align: center; cursor: pointer;
            transition: all 0.2s; margin-bottom: 12px; position: relative;
        }}
        .drop-zone:hover, .drop-zone.drag-over {{
            border-color: rgba(255,0,80,0.4); background: rgba(255,0,80,0.04);
        }}
        .drop-zone p {{ color: rgba(255,255,255,0.4); font-size: 13px; }}
        .drop-zone strong {{ color: rgba(255,255,255,0.7); }}
        .drop-zone input[type="file"] {{
            position: absolute; inset: 0; opacity: 0; cursor: pointer;
            width: 100%; height: 100%;
        }}
        .or-divider {{
            display: flex; align-items: center; gap: 12px;
            margin: 12px 0; color: rgba(255,255,255,0.2); font-size: 12px;
        }}
        .or-divider::before, .or-divider::after {{
            content: ''; flex: 1; height: 1px; background: rgba(255,255,255,0.08);
        }}
        button[type="submit"] {{
            width: 100%; padding: 16px;
            background: linear-gradient(135deg, #ff0050 0%, #ff4d6d 100%);
            border: none; border-radius: 12px; color: #fff;
            font-size: 15px; font-weight: 700; cursor: pointer;
            letter-spacing: 0.3px; transition: all 0.2s;
            box-shadow: 0 4px 24px rgba(255,0,80,0.35); margin-top: 8px;
        }}
        button[type="submit"]:hover {{
            transform: translateY(-1px); box-shadow: 0 8px 32px rgba(255,0,80,0.5);
        }}
        button[type="submit"]:active {{ transform: translateY(0); }}
        .alert {{
            padding: 14px 16px; border-radius: 12px; font-size: 13px;
            margin-bottom: 20px; display: flex; gap: 10px; align-items: flex-start;
        }}
        .alert.success {{
            background: rgba(0,255,128,0.08); border: 1px solid rgba(0,255,128,0.2);
            color: #4fffb0;
        }}
        .alert.error {{
            background: rgba(255,60,60,0.08); border: 1px solid rgba(255,60,60,0.2);
            color: #ff6b6b;
        }}
        .alert .emoji {{ font-size: 18px; }}
        .info-box {{
            background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.07);
            border-radius: 12px; padding: 14px 16px; margin-bottom: 20px;
            font-size: 12px; color: rgba(255,255,255,0.4); line-height: 1.6;
        }}
        .info-box strong {{ color: rgba(255,255,255,0.7); }}
        .stats {{ font-size: 13px; color: rgba(255,255,255,0.5); margin-top: 6px; }}
        .stats span {{ color: #4fffb0; font-weight: 600; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">
            <span class="icon">🍪</span>
            <h1>Cookie Uploader</h1>
            <p>Aggiorna i cookies TikTok sul VPS</p>
            <div class="badge">🔴 TikTok Video Generator</div>
        </div>

        {alert}

        <div class="info-box">
            <strong>Come esportare i cookies:</strong><br>
            1. Apri TikTok nel browser del PC e fai login<br>
            2. Installa l'estensione <strong>Cookie-Editor</strong><br>
            3. Clicca → Export → <strong>Export as JSON</strong><br>
            4. Copia il testo qui sotto o carica il file .json
        </div>

        <form method="POST" action="/" enctype="multipart/form-data">
            <div class="field">
                <label>🔑 Password</label>
                <input type="password" name="password" placeholder="Inserisci la password..." required>
            </div>

            <div class="field">
                <label>📁 Carica file JSON</label>
                <div class="drop-zone" id="dropZone">
                    <p>⬆️ <strong>Trascina qui</strong> il file cookies.json</p>
                    <p style="margin-top:4px">oppure clicca per selezionarlo</p>
                    <input type="file" name="cookie_file" accept=".json,application/json" id="fileInput">
                </div>
                <div class="or-divider">oppure incolla il JSON</div>
            </div>

            <div class="field">
                <label>📋 Incolla JSON dei cookies</label>
                <textarea name="cookie_json" id="cookieJson"
                    placeholder='[{{"name": "sessionid", "value": "...", "domain": ".tiktok.com", ...}}]'></textarea>
            </div>

            <button type="submit">🚀 Aggiorna Cookies sul VPS</button>
        </form>
    </div>

    <script>
        const dropZone  = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const textarea  = document.getElementById('cookieJson');

        dropZone.addEventListener('dragover', e => {{
            e.preventDefault(); dropZone.classList.add('drag-over');
        }});
        dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
        dropZone.addEventListener('drop', e => {{
            e.preventDefault(); dropZone.classList.remove('drag-over');
            const file = e.dataTransfer.files[0];
            if (file) readFile(file);
        }});
        fileInput.addEventListener('change', () => {{
            if (fileInput.files[0]) readFile(fileInput.files[0]);
        }});
        function readFile(file) {{
            const reader = new FileReader();
            reader.onload = e => {{
                textarea.value = e.target.result;
                dropZone.querySelector('p').textContent = '✅ File: ' + file.name;
            }};
            reader.readAsText(file);
        }}
    </script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return render_page()


@app.post("/", response_class=HTMLResponse)
async def upload_cookies(
    request: Request,
    password: str = Form(...),
    cookie_json: str = Form(default=""),
    cookie_file: UploadFile = File(default=None),
):
    client_ip = request.client.host if request.client else "unknown"

    # ── Verifica password ──────────────────────────────────────────────────
    if not hmac.compare_digest(password, UPLOAD_PASSWORD):
        log.warning(f"Password errata da {client_ip}")
        alert = '<div class="alert error"><span class="emoji">❌</span><div><strong>Password errata.</strong></div></div>'
        return HTMLResponse(render_page(alert), status_code=403)

    # ── Ottieni il JSON: prima dal file, poi dal textarea ──────────────────
    json_text = ""

    if cookie_file and cookie_file.filename:
        try:
            raw = await cookie_file.read()
            json_text = raw.decode("utf-8", errors="replace").strip()
            log.info(f"File ricevuto: {cookie_file.filename} ({len(raw)} bytes)")
        except Exception as e:
            log.warning(f"Errore lettura file: {e}")

    if not json_text and cookie_json.strip():
        json_text = cookie_json.strip()

    if not json_text:
        alert = '<div class="alert error"><span class="emoji">❌</span><div><strong>Nessun JSON fornito.</strong> Carica un file o incolla il testo.</div></div>'
        return HTMLResponse(render_page(alert))

    # ── Parsa JSON ─────────────────────────────────────────────────────────
    try:
        cookies_data = json.loads(json_text)
    except json.JSONDecodeError as e:
        alert = f'<div class="alert error"><span class="emoji">❌</span><div><strong>JSON non valido:</strong> {e}</div></div>'
        return HTMLResponse(render_page(alert))

    if not isinstance(cookies_data, list):
        if isinstance(cookies_data, dict) and "cookies" in cookies_data:
            cookies_data = cookies_data["cookies"]
        else:
            alert = '<div class="alert error"><span class="emoji">❌</span><div><strong>Formato errato:</strong> il JSON deve essere una lista <code>[...]</code>.</div></div>'
            return HTMLResponse(render_page(alert))

    # ── Backup del file esistente ──────────────────────────────────────────
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if COOKIES_FILE.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"cookies_backup_{ts}.txt"
        backup_path.write_bytes(COOKIES_FILE.read_bytes())
        log.info(f"Backup salvato: {backup_path}")

    # ── Converti e salva ───────────────────────────────────────────────────
    try:
        netscape_content, converted, skipped = json_to_netscape(cookies_data)
        COOKIES_FILE.write_text(netscape_content, encoding="utf-8")
        log.info(f"✅ cookies.txt aggiornato: {converted} cookies da {client_ip}")
    except Exception as e:
        log.exception(f"Errore conversione: {e}")
        alert = f'<div class="alert error"><span class="emoji">❌</span><div><strong>Errore interno:</strong> {e}</div></div>'
        return HTMLResponse(render_page(alert), status_code=500)

    alert = f"""<div class="alert success">
        <span class="emoji">✅</span>
        <div>
            <strong>Cookies aggiornati con successo!</strong><br>
            <span class="stats">Convertiti: <span>{converted}</span> — Saltati: {skipped} — Backup salvato</span>
        </div>
    </div>"""
    return HTMLResponse(render_page(alert))


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT

    print(f"""
╔══════════════════════════════════════════════════════╗
║         🍪  Cookie Uploader — TikTok VPS            ║
╠══════════════════════════════════════════════════════╣
║  Server in ascolto su : http://0.0.0.0:{port}         ║
║  Accedi dal telefono  : http://<IP_VPS>:{port}        ║
║  Password corrente    : {UPLOAD_PASSWORD:<29}║
║                                                      ║
║  Per cambiare password:                              ║
║    COOKIE_UPLOAD_PASSWORD=nuova python cookie_uploader.py  ║
║  Cookies output       : {str(COOKIES_FILE):<29}║
╚══════════════════════════════════════════════════════╝
""")
    uvicorn.run("cookie_uploader:app", host="0.0.0.0", port=port, reload=False)
