import argparse
import sys
import os
import time
import requests
from pathlib import Path
from rich.console import Console
from dotenv import load_dotenv
from modules.media_server import TemporaryMediaServer
from modules.meta_config import graph_url

console = Console()
load_dotenv()

IG_USER_ID = os.getenv("IG_USER_ID")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
def create_carousel_item(image_path: Path, image_url: str) -> str:

    url_media = graph_url(f"{IG_USER_ID}/media")
    payload = {
        "image_url": image_url,
        "is_carousel_item": "true",
        "access_token": IG_ACCESS_TOKEN
    }

    res = requests.post(url_media, data=payload, timeout=30)
    res.raise_for_status()
    data = res.json()

    if "error" in data:
        console.print(f"[red]Errore creazione item per {image_path.name}: {data['error']['message']}[/]")
        return None

    return data.get("id")

def publish_carousel(image_paths: list[Path], caption: str, media_server: TemporaryMediaServer):
    if not IG_USER_ID or not IG_ACCESS_TOKEN:
        console.print("[red]ERRORE: IG_USER_ID o IG_ACCESS_TOKEN mancanti nel file .env![/]")
        return False

    console.print("\n[dim]1/4 Creazione container per ogni singola immagine (max 10)...[/]")
    item_ids = []
    for img in image_paths[:10]:
        item_id = create_carousel_item(img, media_server.url_for(img))
        if not item_id:
            return False
        item_ids.append(item_id)
        console.print(f"[green]Creato item per {img.name}: {item_id}[/]")
        time.sleep(2) # Pausa tra un upload e l'altro

    console.print("\n[dim]2/4 Creazione container CAROSELLO...[/]")
    url_media = graph_url(f"{IG_USER_ID}/media")
    payload = {
        "media_type": "CAROUSEL",
        "children": ",".join(item_ids),
        "caption": caption,
        "access_token": IG_ACCESS_TOKEN
    }

    res = requests.post(url_media, data=payload, timeout=30)
    res.raise_for_status()
    data = res.json()

    if "error" in data:
        console.print(f"[red]Errore creazione carosello: {data['error']['message']}[/]")
        return False

    carousel_id = data.get("id")
    console.print(f"[green]Container CAROSELLO creato! ID: {carousel_id}[/]")

    console.print("\n[dim]3/4 Attesa elaborazione lato Meta...[/]")
    url_status = graph_url(carousel_id)
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
            console.print("[red]Errore fatale di elaborazione lato Meta.[/]")
            return False
        else:
            console.print(f"[dim]Stato: {status}... riprovo tra 10 secondi[/]")
            time.sleep(10)
    else:
        console.print("[red]Timeout: Meta non ha completato il carosello entro 6 minuti.[/]")
        return False

    console.print("\n[dim]4/4 Invio comando di pubblicazione...[/]")
    url_publish = graph_url(f"{IG_USER_ID}/media_publish")
    publish_payload = {
        "creation_id": carousel_id,
        "access_token": IG_ACCESS_TOKEN
    }

    res_pub = requests.post(url_publish, data=publish_payload, timeout=30)
    res_pub.raise_for_status()
    pub_data = res_pub.json()

    if "error" in pub_data:
        console.print(f"[red]Errore durante la pubblicazione: {pub_data['error']['message']}[/]")
        return False

    post_id = pub_data.get("id")
    console.print(f"\n[bold green]✅ CAROSELLO PUBBLICATO CON SUCCESSO SU INSTAGRAM! (Post ID: {post_id})[/]")
    return True

def main():
    parser = argparse.ArgumentParser(description="Pubblicazione Automatica Caroselli su Instagram tramite API Meta")
    parser.add_argument("--dir", default="temp/carousel", help="Cartella contenente le immagini del carosello (in ordine alfabetico, es. slide_1.jpg)")
    args = parser.parse_args()

    dir_path = Path(args.dir)
    if not dir_path.exists() or not dir_path.is_dir():
        console.print(f"[red]Cartella non trovata o non valida:[/] {dir_path}")
        sys.exit(1)

    # Prendi tutti i .jpg e .png, in ordine
    image_paths = sorted(list(dir_path.glob("*.jpg")) + list(dir_path.glob("*.png")))
    if not image_paths:
        console.print(f"[red]Nessuna immagine (.jpg o .png) trovata in {dir_path}[/]")
        sys.exit(1)

    # Leggi la caption
    caption_path = Path("scripts/ig_caption.txt")
    if not caption_path.exists():
        console.print("[yellow]Nessuna caption trovata in scripts/ig_caption.txt, uso fallback.[/]")
        caption = "Scorri per leggere 👉 #carosello #divulgazione"
    else:
        caption = caption_path.read_text(encoding="utf-8").strip()

    console.print(f"[bold cyan]Trovate {len(image_paths)} immagini per il carosello.[/]")
    console.print(f"[dim]Caption: {caption[:50]}...[/dim]")

    with TemporaryMediaServer(image_paths) as media_server:
        if not publish_carousel(image_paths, caption, media_server):
            sys.exit(1)

if __name__ == "__main__":
    main()
