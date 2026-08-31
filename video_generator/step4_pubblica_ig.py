import argparse
import sys
import os
import time
from pathlib import Path
from rich.console import Console
from playwright.sync_api import sync_playwright

console = Console()
CHROME_PROFILE_DIR = Path(__file__).parent / "chrome_profile_ig"

def upload_con_profilo_ig(video_path: Path, description: str, headless: bool = True) -> bool:
    console.print(f"[cyan]Avvio Automazione Browser per Instagram (Reels/Video)...[/]")
    console.print(f"[dim]Profilo: {CHROME_PROFILE_DIR}[/]")

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
                locale="it-IT"
            )

            page = context.new_page()

            # 1. Vai alla home di Instagram
            console.print("[dim]Navigazione su Instagram...[/]")
            page.goto("https://www.instagram.com/", wait_until="networkidle", timeout=30000)
            time.sleep(3)

            # Controllo Login
            if page.locator("input[name='username']").is_visible():
                console.print("[red]⚠ Sessione scaduta o non loggato su Instagram.[/]")
                console.print("[yellow]Esegui: ./setup_ig_login.sh per rifare il login.[/]")
                context.close()
                return False

            # 2. Clicca su "Crea" (Nuovo post)
            console.print("[dim]Clicco su 'Crea' (Nuovo post)...[/]")
            try:
                # Cerca il pulsante con aria-label o testo
                create_btn = page.locator("svg[aria-label='Nuovo post'], svg[aria-label='New post']").locator("..").first
                create_btn.click(force=True)
            except Exception as e:
                console.print(f"[yellow]Pulsante 'Crea' non trovato al primo colpo, cerco il testo...[/]")
                page.locator("span:text-is('Crea'), span:text-is('Create')").first.click()

            time.sleep(2)

            # 3. Intercetta la finestra di upload e carica il file
            console.print(f"[dim]Caricamento video: {video_path.name}[/]")
            try:
                with page.expect_file_chooser() as fc_info:
                    page.locator("button:has-text('Seleziona dal computer'), button:has-text('Select from computer')").click()
                file_chooser = fc_info.value
                file_chooser.set_files(str(video_path))
            except Exception as e:
                page.screenshot(path="ig_debug_upload.png")
                console.print(f"[red]Errore durante l'apertura del selettore file. Screenshot salvato.[/red]")
                raise e

            time.sleep(3)

            # 4. Formato (Proporzioni) -> Originale per evitare crop
            console.print("[dim]Imposto proporzioni originali (9:16)...[/]")
            try:
                page.locator("button[aria-label='Seleziona le proporzioni'], button[aria-label='Select crop']").click()
                time.sleep(1)
                page.locator("span:has-text('Originale'), span:has-text('Original')").locator("..").click()
            except Exception:
                console.print("[dim yellow]Non è stato possibile cliccare sulle proporzioni (potrebbe non essere necessario).[/]")

            time.sleep(2)

            # 5. Clicca "Avanti" (Next) - Passo 1
            console.print("[dim]Avanti (Step 1)...[/]")
            page.locator("div[role='button']:has-text('Avanti'), div[role='button']:has-text('Next')").click()
            time.sleep(2)

            # 6. Clicca "Avanti" (Next) - Passo 2 (Copertina/Filtri)
            console.print("[dim]Avanti (Step 2)...[/]")
            page.locator("div[role='button']:has-text('Avanti'), div[role='button']:has-text('Next')").click()
            time.sleep(2)

            # 7. Inserisci la caption
            console.print("[dim]Inserimento Didascalia...[/]")
            try:
                desc_box = page.locator("div[aria-label='Scrivi una didascalia...'], div[aria-label='Write a caption...']").first
                desc_box.click()
                page.keyboard.type(description, delay=30)
            except Exception as e:
                console.print(f"[yellow]⚠ Didascalia non impostata, campo non trovato: {e}[/]")

            time.sleep(2)

            # 8. Clicca "Condividi" (Share)
            console.print("[dim]Clicco su Condividi...[/]")
            try:
                share_btn = page.locator("div[role='button']:has-text('Condividi'), div[role='button']:has-text('Share')")
                share_btn.click()
            except Exception as e:
                page.screenshot(path="ig_debug_share.png")
                console.print(f"[red]Errore sul bottone Condividi. Screenshot salvato.[/red]")
                raise e

            # 9. Attendi il termine dell'upload
            console.print("[dim]Attesa caricamento su Instagram (potrebbe volerci 1-2 minuti)...[/]")
            try:
                page.wait_for_selector("img[alt='Il tuo post è stato condiviso.'], img[alt='Your post has been shared.'], span:has-text('Il tuo post è stato condiviso.'), span:has-text('Il tuo reel è stato condiviso.')", timeout=120000)
                console.print("[bold green]✅ Popup di condivisione rilevato![/]")
            except Exception:
                console.print("[red]Conferma di pubblicazione non ricevuta entro il timeout.[/]")
                page.screenshot(path="ig_after_post.png")
                context.close()
                return False

            time.sleep(3)
            context.close()
            return True

    except Exception as e:
        console.print(f"[bold red]Errore fatale in upload IG: {e}[/]")
        return False

def main():
    parser = argparse.ArgumentParser(description="Pubblicazione Automatica su Instagram Reels")
    parser.add_argument("--video", default="output/carosello_finale.mp4", help="Video da caricare")
    parser.add_argument("--script", default="scripts/script_carosello.txt", help="File di testo per generare la caption")
    parser.add_argument("--show-browser", action="store_true", help="Mostra il browser durante l'upload")
    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        console.print(f"[red]Video non trovato:[/] {video_path}")
        sys.exit(1)

    # Legge o genera la caption
    # Per ora prendiamo direttamente il testo se c'è
    caption = "Nuovo post! ✨ #creativo"
    script_path = Path(args.script)
    if script_path.exists():
        testo = script_path.read_text(encoding="utf-8")
        caption = f"{testo[:150]}...\n\n#reels #consapevolezza #crescitapersonale #mindset #benessere"

    # Se esiste la caption di tiktok generata da AGY, usiamo quella (o facciamo una logica simile)
    caption_ig_path = Path("scripts/ig_caption.txt")
    if caption_ig_path.exists():
        caption = caption_ig_path.read_text(encoding="utf-8").strip()

    console.print("[bold green]Descrizione IG (Caption):[/]")
    console.print(caption)
    console.print("-" * 50)

    headless = not args.show_browser
    success = upload_con_profilo_ig(video_path, caption, headless)

    if success:
        console.print("[bold green]✓ VIDEO (REEL) PUBBLICATO CON SUCCESSO SU INSTAGRAM![/]")
    else:
        console.print("[bold red]✖ UPLOAD IG FALLITO — controlla i file ig_debug_*.png per dettagli.[/]")
        sys.exit(1)

if __name__ == "__main__":
    main()
