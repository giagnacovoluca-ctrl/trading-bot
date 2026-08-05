import argparse
import sys
import os
from pathlib import Path
from rich.console import Console

from tiktok_uploader.upload import upload_video
from dotenv import load_dotenv

load_dotenv()
console = Console()

def genera_metadata_tiktok(script_text: str, mode: str = "promo") -> str:
    """Legge la descrizione e hashtag per TikTok da file generato da Antigravity."""
    caption_path = Path("scripts/tiktok_caption.txt")
    if caption_path.exists():
        try:
            return caption_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            console.print(f"[red]Errore lettura caption:[/] {e}")
            
    # Fallback
    return "Scopri i segreti in questo video! 📖 Clicca il link in bio per il manuale. #imparacontiktok #libri"

def main():
    parser = argparse.ArgumentParser(description="Step 4: Pubblicazione Automatica su TikTok")
    parser.add_argument("--video", default="output/video_finale.mp4", help="Video da caricare")
    parser.add_argument("--script", default="scripts/script_generato.txt", help="Copione generato")
    parser.add_argument("--cookies", default="cookies.txt", help="File cookies.txt estratto da TikTok web")
    parser.add_argument("--mode", default="promo", choices=["promo", "virale"], help="Modalità video")
    
    args = parser.parse_args()
    
    video_path = Path(args.video)
    if not video_path.exists():
        console.print(f"[red]Video non trovato:[/] {video_path}")
        sys.exit(1)
        
    cookies_path = Path(args.cookies)
    if not cookies_path.exists():
        console.print(f"[red]ERRORE:[/] File '{args.cookies}' non trovato!")
        console.print("[yellow]Per caricare automaticamente su TikTok, devi esportare i cookie dal tuo browser (usa un'estensione come 'Get cookies.txt LOCALLY' mentre sei loggato su tiktok.com) e salvarli nella cartella del progetto come 'cookies.txt'[/]")
        sys.exit(1)
        
    script_path = Path(args.script)
    script_text = ""
    if script_path.exists():
        script_text = script_path.read_text(encoding="utf-8")
        
    console.print(f"[cyan]Generazione Titolo, Descrizione e Hashtag con Gemini (Modo: {args.mode.upper()})...[/]")
    tiktok_caption = genera_metadata_tiktok(script_text, args.mode)
    
    console.print("[bold green]Descrizione generata:[/]")
    console.print(tiktok_caption)
    console.print("-" * 50)
    
    console.print("[cyan]Inizio caricamento su TikTok in background...[/]")
    try:
        # tiktok-uploader usa Playwright in headless mode per caricare il video bypassando le API
        upload_video(
            str(video_path), 
            description=tiktok_caption, 
            cookies=str(cookies_path),
            headless=True
        )
        console.print("[bold green]✓ VIDEO PUBBLICATO CON SUCCESSO SU TIKTOK![/]")
    except Exception as e:
        console.print(f"[bold red]✖ ERRORE DURANTE L'UPLOAD:[/] {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
