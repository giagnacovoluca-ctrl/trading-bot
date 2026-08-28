import sys
import os
import time
import requests
import urllib.parse
import urllib.request
import subprocess
from pathlib import Path
from rich.console import Console
from dotenv import load_dotenv
import random
import json
from PIL import Image, ImageDraw, ImageFont
from modules.media_server import TemporaryMediaServer
from modules.meta_config import graph_url

console = Console()
load_dotenv()

IG_USER_ID = os.getenv("IG_USER_ID")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
def call_agy(prompt: str) -> str:
    try:
        result = subprocess.run(
            ['agy', '--dangerously-skip-permissions', '--print', prompt],
            text=True, capture_output=True, timeout=120, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        raise RuntimeError(f"Errore AGY: {e}") from e

def wrap_text(text, font, max_width, draw):
    words = text.split()
    lines = []
    current_line = []
    for word in words:
        current_line.append(word)
        test_line = " ".join(current_line)
        bbox = draw.textbbox((0, 0), test_line, font=font)
        w = bbox[2] - bbox[0]
        if w > max_width:
            current_line.pop()
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [word]
    if current_line:
        lines.append(" ".join(current_line))
    return lines

def generate_story_image() -> Path:
    console.print("[cyan]1. Generazione testi per la Storia via AGY...[/]")

    EBOOKS = ["Cibo/Salute", "Meditazione", "Nervo Vago/Stress", "Epigenetica/DNA", "Acqua/Idratazione"]
    ebook = random.choice(EBOOKS)

    prompt = f"""
Sei un social media manager. Crea un testo d'impatto per una Storia di Instagram (formato verticale) per promuovere l'ebook: '{ebook}'.
La storia deve avere:
1. 'titolo': una domanda o affermazione che cattura subito (max 6 parole, es. "Ti senti sempre stanco?").
2. 'sottotitolo': una call to action per i DM usando ManyChat (es. "Rispondi GUIDA a questa storia per ricevere il manuale.").

Restituisci SOLO un file JSON valido:
{{
  "titolo": "...",
  "sottotitolo": "..."
}}
"""
    raw_json = call_agy(prompt)
    import re
    match = re.search(r'\{.*\}', raw_json, re.DOTALL)

    if match is None:
        raise RuntimeError("AGY non ha restituito JSON per la storia")
    try:
        data = json.loads(match.group(0))
        titolo = data['titolo'].strip()
        sottotitolo = data['sottotitolo'].strip()
    except (json.JSONDecodeError, KeyError, AttributeError) as e:
        raise RuntimeError(f"JSON storia non valido: {e}") from e
    if not titolo or len(titolo.split()) > 6 or not sottotitolo:
        raise RuntimeError("Testo storia fuori dai limiti richiesti")

    console.print(f"[bold]Titolo:[/] {titolo}\n[bold]Sottotitolo:[/] {sottotitolo}")

    out_dir = Path("temp/stories")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"story_{int(time.time())}.jpg"

    console.print("[cyan]2. Generazione Sfondo (Pollinations.ai)...[/]")
    encoded_prompt = urllib.parse.quote(f"Dark aesthetic minimal elegant background for {titolo[:30]}, 9:16 vertical, no text")
    bg_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1920&model=flux&nologo=true"

    try:
        req = urllib.request.Request(bg_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as response, open(out_path, 'wb') as out_file:
            out_file.write(response.read())
    except:
        console.print("[yellow]Pollinations fallito, uso sfondo nero sfumato.[/]")
        img = Image.new('RGB', (1080, 1920), (20, 20, 30))
        img.save(out_path)

    console.print("[cyan]3. Sovrascrizione testo sull'immagine...[/]")
    img = Image.open(out_path).convert("RGBA")

    # Forza il ridimensionamento a 1080x1920 nel caso l'API restituisca un'immagine più piccola
    img = img.resize((1080, 1920), Image.Resampling.LANCZOS)

    # Crea un layer scuro per far leggere meglio il testo
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 150))
    img = Image.alpha_composite(img, overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Usa Montserrat o fallback
    font_path = Path("assets/fonts/Montserrat-Bold.ttf")
    try:
        font_titolo = ImageFont.truetype(str(font_path), 80)
        font_sub = ImageFont.truetype(str(font_path), 45)
    except:
        font_titolo = ImageFont.load_default()
        font_sub = ImageFont.load_default()

    # Disegna Titolo (Centro alto)
    lines_titolo = wrap_text(titolo, font_titolo, 850, draw)
    y_text = 450
    for line in lines_titolo:
        # Usa l'ancoraggio centrale 'mm' per il bounding box e il disegno
        bbox = draw.textbbox((1080/2, y_text), line, font=font_titolo, anchor="mm")

        # Ombra
        draw.text((1080/2 + 4, y_text + 4), line, font=font_titolo, fill="black", anchor="mm")
        # Testo
        draw.text((1080/2, y_text), line, font=font_titolo, fill="white", anchor="mm")

        y_text += (bbox[3] - bbox[1]) + 25

    # Disegna CTA (Centro Basso, alzato per stare nella Safe Zone di Instagram)
    y_sub = 1150
    lines_sub = wrap_text(sottotitolo, font_sub, 800, draw)
    for line in lines_sub:
        bbox = draw.textbbox((1080/2, y_sub), line, font=font_sub, anchor="mm")

        # Sfondo per la CTA
        padding = 35
        draw.rounded_rectangle(
            [bbox[0] - padding, bbox[1] - padding, bbox[2] + padding, bbox[3] + padding],
            radius=20, fill=(200, 40, 40, 255)
        )
        draw.text((1080/2, y_sub), line, font=font_sub, fill="white", anchor="mm")

        y_sub += (bbox[3] - bbox[1]) + 30

    img.save(out_path, quality=90)
    console.print(f"[green]Immagine storia salvata in: {out_path}[/]")
    return out_path

def publish_story(image_path: Path, image_url: str) -> bool:
    console.print("\n[cyan]4. Pubblicazione su Instagram Stories API...[/]")
    if not IG_USER_ID or not IG_ACCESS_TOKEN:
        console.print("[red]ERRORE: Credenziali mancanti![/]")
        return False

    url_media = graph_url(f"{IG_USER_ID}/media")
    payload = {
        "image_url": image_url,
        "media_type": "STORIES",
        "access_token": IG_ACCESS_TOKEN
    }

    # 1. Crea Container
    res = requests.post(url_media, data=payload, timeout=30)
    res.raise_for_status()
    data = res.json()
    if "error" in data:
        console.print(f"[red]Errore creazione storia: {data['error']['message']}[/]")
        return False

    creation_id = data.get("id")
    console.print(f"[dim]Container Storia creato: {creation_id}. Attesa elaborazione...[/]")

    # 2. Attesa
    url_status = graph_url(creation_id)
    for _ in range(72):
        res_status = requests.get(url_status, params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN}, timeout=30)
        res_status.raise_for_status()
        status_data = res_status.json()
        if status_data.get("status_code", "").upper() == "FINISHED":
            break
        elif status_data.get("status_code", "").upper() == "ERROR":
            return False
        time.sleep(5)
    else:
        console.print("[red]Timeout: Meta non ha completato la storia entro 6 minuti.[/]")
        return False

    # 3. Pubblicazione
    url_publish = graph_url(f"{IG_USER_ID}/media_publish")
    pub_payload = {"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN}
    res_pub = requests.post(url_publish, data=pub_payload, timeout=30)
    res_pub.raise_for_status()
    pub_data = res_pub.json()

    if "error" in pub_data:
        console.print(f"[red]Errore pubblicazione storia: {pub_data['error']['message']}[/]")
        return False

    console.print(f"[bold green]✅ STORIA PUBBLICATA CON SUCCESSO! (ID: {pub_data.get('id')})[/]")
    return True

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-publish", action="store_true")
    args = parser.parse_args()
    image_path = generate_story_image()
    if args.no_publish:
        console.print("[yellow]Modalità --no-publish: storia generata senza upload.[/]")
        return
    with TemporaryMediaServer([image_path]) as media_server:
        if not publish_story(image_path, media_server.url_for(image_path)):
            raise SystemExit(1)

if __name__ == "__main__":
    main()
