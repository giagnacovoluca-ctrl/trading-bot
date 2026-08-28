import argparse
import sys
import os
import time
import requests
from pathlib import Path
from rich.console import Console
from dotenv import load_dotenv
import subprocess
from modules.media_server import TemporaryMediaServer
from modules.meta_config import graph_url

console = Console()
load_dotenv()

IG_USER_ID = os.getenv("IG_USER_ID")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
def publish_reel(video_path: Path, caption: str, video_url: str):
    if not IG_USER_ID or not IG_ACCESS_TOKEN:
        console.print("[red]ERRORE: IG_USER_ID o IG_ACCESS_TOKEN mancanti nel file .env![/]")
        return False

    # URL Pubblico del video servito dal nostro server locale
    console.print(f"[cyan]Video servito temporaneamente a Meta su: {video_url}[/]")

    # 1. Creazione del Container
    console.print("\n[dim]1/3 Richiesta creazione Container a Meta...[/]")
    url_media = graph_url(f"{IG_USER_ID}/media")
    payload = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": IG_ACCESS_TOKEN
    }

    res = requests.post(url_media, data=payload, timeout=30)
    res.raise_for_status()
    data = res.json()

    if "error" in data:
        console.print(f"[red]Errore creazione container: {data['error']['message']}[/]")
        return False

    container_id = data.get("id")
    console.print(f"[green]Container creato! ID: {container_id}[/]")

    # 2. Attesa elaborazione
    console.print("\n[dim]2/3 Attesa elaborazione video lato Meta... (potrebbe volerci 1-2 minuti)[/]")
    url_status = graph_url(container_id)
    status_params = {
        "fields": "status_code",
        "access_token": IG_ACCESS_TOKEN
    }

    for _ in range(36):
        res_status = requests.get(url_status, params=status_params, timeout=30)
        res_status.raise_for_status()
        status_data = res_status.json()

        if "error" in status_data:
            console.print(f"[red]Errore controllo stato: {status_data['error']['message']}[/]")
            return False

        status = status_data.get("status_code", "").upper()
        if status == "FINISHED":
            console.print("[green]Elaborazione completata![/]")
            break
        elif status == "ERROR":
            console.print("[red]Errore fatale di elaborazione lato Meta (video non valido).[/]")
            return False
        else:
            console.print(f"[dim]Stato: {status}... riprovo tra 10 secondi[/]")
            time.sleep(10)
    else:
        console.print("[red]Timeout: Meta non ha completato il Reel entro 6 minuti.[/]")
        return False

    # 3. Pubblicazione
    console.print("\n[dim]3/3 Invio comando di pubblicazione...[/]")
    url_publish = graph_url(f"{IG_USER_ID}/media_publish")
    publish_payload = {
        "creation_id": container_id,
        "access_token": IG_ACCESS_TOKEN
    }

    res_pub = requests.post(url_publish, data=publish_payload, timeout=30)
    res_pub.raise_for_status()
    pub_data = res_pub.json()

    if "error" in pub_data:
        console.print(f"[red]Errore durante la pubblicazione: {pub_data['error']['message']}[/]")
        return False

    post_id = pub_data.get("id")
    console.print(f"\n[bold green]✅ REEL PUBBLICATO CON SUCCESSO SU INSTAGRAM! (Post ID: {post_id})[/]")
    return True

def genera_metadata_ig(script_text: str, mode: str = "virale") -> str:
    """Legge la descrizione e hashtag per IG da file generato da Antigravity o lo crea al volo."""
    caption_path = Path("scripts/ig_caption.txt")
    if caption_path.exists():
        try:
            return caption_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            console.print(f"[red]Errore lettura caption:[/] {e}")

    console.print("[dim]Nessun file ig_caption.txt trovato, lo genero al volo tramite AGY CLI...[/dim]")
    prompt = f"Sei un social media manager per Instagram. Crea una caption per un Reel basato su questo copione. Usa hashtag virali appropriati per Instagram (molto ricercati, max 15). Metti sempre una CTA forte per commentare o salvare il post. Se la modalità è 'promo', INCLUDI ASSOLUTAMENTE la CTA di cliccare il link in bio. Restituisci SOLO la caption.\n\nMODALITA: {mode}\nCOPIONE:\n{script_text}"
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
        console.print(f"[red]Errore AGY nella generazione caption IG:[/] {e}")

    # Fallback
    return "Scopri di più in questo nuovo Reel! ✨ Clicca il link in bio per approfondire. #crescitapersonale #mindset #consapevolezza"

def main():
    parser = argparse.ArgumentParser(description="Pubblicazione Automatica su Instagram Reels tramite API Meta")
    parser.add_argument("--video", default="output/carosello_finale.mp4", help="Video da caricare")
    parser.add_argument("--script", default="scripts/script_carosello.txt", help="File di testo per generare la caption")
    parser.add_argument("--mode", default="virale", help="Modalità video (promo, virale)")
    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        console.print(f"[red]Video non trovato:[/] {video_path}")
        sys.exit(1)

    script_text = ""
    script_path = Path(args.script)
    if script_path.exists():
        script_text = script_path.read_text(encoding="utf-8")

    caption = genera_metadata_ig(script_text, args.mode)

    console.print("[bold green]Descrizione IG (Caption):[/]")
    console.print(caption)
    console.print("-" * 50)

    # Avvia il server temporaneo
    with TemporaryMediaServer([video_path]) as media_server:
        if not publish_reel(video_path, caption, media_server.url_for(video_path)):
            sys.exit(1)

if __name__ == "__main__":
    main()
