import argparse
import sys
import os
import re
import time
import tempfile
import json
from pathlib import Path
from rich.console import Console
from modules.script_quality import validate_publication_text
from modules.content_tracking import prepare_tracked_caption

from dotenv import load_dotenv

load_dotenv()
console = Console()

# Cartella profilo Chrome persistente — creata da setup_tiktok_login.sh
CHROME_PROFILE_DIR = Path(__file__).parent / "chrome_profile"
RECEIPT_PATH = Path(__file__).parent / 'output/tiktok_receipt.json'


def genera_metadata_tiktok(
    script_text: str,
    mode: str = "promo",
    use_existing_caption: bool = False,
) -> str:
    """Legge la descrizione e hashtag per TikTok da file generato da Antigravity."""
    caption_path = Path("scripts/tiktok_caption.txt")
    if use_existing_caption and caption_path.exists():
        try:
            return caption_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            console.print(f"[red]Errore lettura caption:[/] {e}")

    import subprocess
    console.print("[dim]Nessun file tiktok_caption.txt trovato, lo genero al volo tramite AGY CLI...[/dim]")
    prompt = f"""Sei un social media manager per TikTok. Crea una breve caption per un post basato su questo copione. Usa hashtag appropriati. Se la modalità è 'promo', conserva nella caption la CTA finale del copione ESATTAMENTE com'è scritta: non trasformare un'anteprima in PDF o download, non promettere email o DM e non sostituire la destinazione. Mantieni un tono educativo e accurato. Non usare formule come 'prova definitiva', 'inconfutabile', 'che non ti dicono', 'verità nascosta' o altre certezze assolute. Non aggiungere numeri, confronti o fatti che non siano già sostenuti da una fonte precisa nel copione. Restituisci SOLO la caption.

MODALITA: {mode}
COPIONE:
{script_text}"""
    try:
        result = subprocess.run(
            ["agy", "--dangerously-skip-permissions", "--print", prompt], 
            capture_output=True, 
            text=True
        )
        if result.returncode == 0:
            caption = result.stdout.strip()
            # Salva per cache/debug
            caption_path.parent.mkdir(parents=True, exist_ok=True)
            caption_path.write_text(caption, encoding="utf-8")
            return caption
    except Exception as e:
        console.print(f"[red]Errore AGY nella generazione caption:[/] {e}")

    # Fallback estremo
    return "Scopri i segreti in questo video! 📖 Clicca il link in bio per scoprire di più. #imparacontiktok #crescita"


def normalize_cookies(cookies_path: Path) -> Path:
    """Crea un file cookies temporaneo rimuovendo il prefisso '#HttpOnly_' dal dominio.
    Playwright rifiuta quel prefisso come 'Invalid cookie fields' anche se è
    formato Netscape corretto. Restituisce il path del file temporaneo."""
    content = cookies_path.read_text(encoding="utf-8")
    normalized = re.sub(r'^#HttpOnly_', '', content, flags=re.MULTILINE)
    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.txt', delete=False, encoding='utf-8'
    )
    tmp.write(normalized)
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


def upload_con_profilo(video_path: Path, description: str, headless: bool = True) -> bool | None:
    """Upload usando il profilo Chrome persistente (metodo principale).
    Ritorna True se successo, False se fallito."""
    from playwright.sync_api import sync_playwright

    console.print(f"[cyan]Modalità: Profilo browser persistente[/]")
    console.print(f"[dim]Profilo: {CHROME_PROFILE_DIR}[/]")

    upload_url = "https://www.tiktok.com/creator-center/upload?lang=en"
    submitted = False
    published_ids = set()

    def capture_publish_response(response):
        if '/publish' not in response.url or response.status != 200:
            return
        try:
            payload = response.json()
            if payload.get('status_code', payload.get('statusCode', 0)) not in (0, '0'):
                return
            data = payload.get('data') or payload
            post_id = str(data.get('item_id') or data.get('aweme_id') or '')
            if re.fullmatch(r'\d{15,25}', post_id):
                published_ids.add(post_id)
        except Exception:
            return

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(CHROME_PROFILE_DIR),
                executable_path="/usr/bin/google-chrome",
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 900},
            )

            page = context.new_page()
            page.on('response', capture_publish_response)

            # Vai alla pagina upload
            console.print("[dim]Navigazione alla pagina upload...[/]")
            page.goto(upload_url, wait_until="domcontentloaded", timeout=30000)

            # Controlla se siamo stati reindirizzati al login
            time.sleep(3)
            current_url = page.url
            if "login" in current_url:
                console.print("[red]⚠ Sessione scaduta — il profilo necessita di un nuovo login.[/]")
                console.print("[yellow]Esegui: ./setup_tiktok_login.sh[/]")
                context.close()
                return False

            # Attendi l'iframe di upload o il pulsante di upload
            console.print("[dim]Attesa modulo upload...[/]")
            try:
                page.wait_for_selector("iframe", timeout=5000)
                container = page.frame_locator("iframe").first
                console.print("[dim]Trovato iframe (layout vecchio)[/dim]")
            except Exception:
                container = page
                console.print("[dim]Nessun iframe trovato (layout nuovo)[/dim]")

            try:
                file_input = container.locator("input[type='file']").first
                file_input.set_input_files(str(video_path))
                console.print(f"[dim]Video caricato: {video_path.name}[/]")
            except Exception as e:
                page.screenshot(path="tiktok_debug_upload_fail.png")
                console.print(f"[red]Errore caricamento file. Screenshot salvato.[/red]")
                raise e

            # Attendi elaborazione video (barra di caricamento)
            time.sleep(5)
            page.wait_for_timeout(10000)

            # Imposta descrizione
            try:
                desc_box = container.locator("[data-contents='true'], .public-DraftEditor-content").first
                desc_box.click(force=True)
                # Seleziona tutto e sostituisci
                page.keyboard.press("Control+a")
                page.keyboard.type(description, delay=30)
                console.print("[dim]Descrizione impostata.[/]")
            except Exception as e:
                console.print(f"[yellow]⚠ Descrizione non impostata: {e}[/]")

            time.sleep(3)

            # Clicca Pubblica
            try:
                post_btn = container.locator("button:has-text('Post'), button:has-text('Pubblica')").last
                submitted = True
                post_btn.evaluate("node => node.click()")
                console.print("[dim]Pulsante Pubblica cliccato (via JS). Controllo popup di conferma...[/]")
                time.sleep(5)
                
                try:
                    post_now_btn = page.locator("button:has-text('Post now'), button:has-text('Pubblica ora')").last
                    if post_now_btn.count() > 0 and post_now_btn.is_visible():
                        post_now_btn.evaluate("node => node.click()")
                        console.print("[bold yellow]Popup 'Post now' bypassato![/]")
                except Exception:
                    pass
                
                time.sleep(30)
                page.screenshot(path="tiktok_after_post.png")
            except Exception as e:
                console.print(f"[yellow]⚠ Pulsante pubblica non trovato: {e}[/]")
                context.close()
                return None if submitted else False

            # Salva i cookie aggiornati (auto-refresh sessione)
            _salva_cookies_aggiornati(context)
            body = page.inner_text('body')
            confirmed = bool(published_ids) or bool(re.search(r'(?:your video (?:has been|is) (?:uploaded|published)|video (?:pubblicato|caricato) con successo|post published)', body, re.I))
            if len(published_ids) == 1:
                RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
                RECEIPT_PATH.write_text(json.dumps({'media_id': next(iter(published_ids)), 'video_file': str(video_path), 'created_at': time.time()}))
            context.close()
            return True if confirmed else None

    except Exception as e:
        console.print(f"[red]Errore upload con profilo: {e}[/]")
        return None if submitted else False


def _salva_cookies_aggiornati(context) -> None:
    """Salva i cookie aggiornati dal browser nel cookies.txt (mantiene la sessione fresca)."""
    try:
        cookies = context.cookies()
        lines = ["# Netscape HTTP Cookie File", f"# Aggiornato automaticamente da step4_pubblica.py", ""]
        for c in cookies:
            domain = c.get("domain", "")
            http_only = c.get("httpOnly", False)
            prefix = "#HttpOnly_" if http_only else ""
            secure = "TRUE" if c.get("secure", False) else "FALSE"
            expires = int(c.get("expires", 0)) if c.get("expires", 0) > 0 else 0
            name = c.get("name", "")
            value = c.get("value", "")
            path = c.get("path", "/")
            lines.append(f"{prefix}{domain}\tTRUE\t{path}\t{secure}\t{expires}\t{name}\t{value}")

        cookies_path = Path(__file__).parent / "cookies.txt"
        cookies_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        console.print(f"[dim]✓ Cookie sessione aggiornati in cookies.txt[/]")
    except Exception as e:
        console.print(f"[dim yellow]⚠ Auto-salvataggio cookie fallito: {e}[/]")


def upload_con_cookies(video_path: Path, description: str, cookies_path: Path, headless: bool = True) -> bool:
    """Fallback: upload usando cookies.txt (metodo legacy)."""
    from tiktok_uploader.upload import upload_video

    console.print("[yellow]Modalità fallback: cookies.txt[/]")
    normalized_cookies = normalize_cookies(cookies_path)
    try:
        failed = upload_video(
            str(video_path),
            description=description,
            cookies=str(normalized_cookies),
            headless=headless
        )
        return not bool(failed)
    finally:
        try:
            normalized_cookies.unlink(missing_ok=True)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Step 4: Pubblicazione Automatica su TikTok")
    parser.add_argument("--video", default="output/video_finale.mp4", help="Video da caricare")
    parser.add_argument("--script", default="scripts/script_generato.txt", help="Copione generato")
    parser.add_argument("--cookies", default="cookies.txt", help="File cookies.txt (fallback)")
    parser.add_argument("--mode", default="promo", choices=["promo", "virale", "bastian"], help="Modalità video")
    parser.add_argument("--show-browser", action="store_true", help="Mostra il browser durante l'upload")
    parser.add_argument("--force-cookies", action="store_true", help="Forza uso cookies.txt anche se il profilo esiste")
    parser.add_argument(
        "--use-existing-caption",
        action="store_true",
        help="Usa scripts/tiktok_caption.txt preparata dalla pipeline corrente",
    )

    args = parser.parse_args()
    RECEIPT_PATH.unlink(missing_ok=True)

    video_path = Path(args.video)
    if not video_path.exists():
        console.print(f"[red]Video non trovato:[/] {video_path}")
        sys.exit(1)

    script_path = Path(args.script)
    script_text = ""
    if script_path.exists():
        script_text = script_path.read_text(encoding="utf-8")

    console.print(f"[cyan]Generazione descrizione (Modo: {args.mode.upper()})...[/]")
    tiktok_caption = genera_metadata_tiktok(
        script_text,
        args.mode,
        use_existing_caption=args.use_existing_caption,
    )
    tiktok_caption, campaign = prepare_tracked_caption(
        tiktok_caption, "tiktok", script_text, args.mode
    )
    console.print(f"[dim]Campagna tracciata: {campaign['campaign_id']}[/]")

    caption_issues = validate_publication_text(tiktok_caption)
    if caption_issues:
        console.print("[bold red]✖ Pubblicazione interrotta: caption a rischio disinformazione.[/]")
        for issue in caption_issues:
            console.print(f"[red]- {issue}[/]")
        console.print("[yellow]Correggi o elimina scripts/tiktok_caption.txt e riprova.[/]")
        sys.exit(1)

    console.print("[bold green]Descrizione generata:[/]")
    console.print(tiktok_caption)
    console.print("-" * 50)

    headless = not args.show_browser
    success = False

    # Metodo 1: profilo persistente (preferito)
    usa_profilo = CHROME_PROFILE_DIR.exists() and not args.force_cookies
    if usa_profilo:
        console.print(f"[cyan]Inizio upload con profilo persistente...[/]")
        success = upload_con_profilo(video_path, tiktok_caption, headless=headless)
    
    # Metodo 2: cookies.txt (fallback)
    if success is None:
        console.print('[red]Invio effettuato ma conferma non verificabile. Controllare TikTok prima di riprovare: nessun secondo upload automatico.[/]')
        sys.exit(2)
    if not success:
        cookies_path = Path(args.cookies)
        if cookies_path.exists():
            if usa_profilo:
                console.print("[yellow]Profilo fallito, tentativo con cookies.txt...[/]")
            console.print(f"[cyan]Inizio upload con cookies.txt...[/]")
            success = upload_con_cookies(video_path, tiktok_caption, cookies_path, headless=headless)
        else:
            console.print(f"[red]Nessun metodo di autenticazione disponibile![/]")
            console.print("[yellow]Esegui ./setup_tiktok_login.sh per configurare il profilo persistente.[/]")

    if success:
        console.print("[bold green]✓ VIDEO PUBBLICATO CON SUCCESSO SU TIKTOK![/]")
    else:
        console.print("[bold red]✖ UPLOAD FALLITO — controlla i log per dettagli.[/]")
        sys.exit(1)


if __name__ == "__main__":
    main()
