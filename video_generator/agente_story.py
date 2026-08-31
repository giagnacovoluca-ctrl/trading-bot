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
import re
from PIL import Image, ImageDraw, ImageFont
from modules.media_server import TemporaryMediaServer
from modules.meta_config import graph_url
from modules.content_tracking import create_campaign, save_campaign
from modules.feedback_loop import log_upload
from modules.ebook_catalog import load_ebook_catalog

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

def validate_story_content(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("La storia deve essere un oggetto JSON")
    fields = {key: str(data.get(key, "")).strip() for key in ("titolo", "insight", "azione")}
    if not 4 <= len(fields["titolo"].split()) <= 9:
        raise ValueError("Il titolo deve contenere da 4 a 9 parole")
    if not 18 <= len(fields["insight"].split()) <= 38:
        raise ValueError("L'insight deve contenere da 18 a 38 parole")
    if not 7 <= len(fields["azione"].split()) <= 18:
        raise ValueError("L'azione deve contenere da 7 a 18 parole")
    combined = " ".join(fields.values()).lower()
    risky_patterns = (
        r"\bnon (?:idrata|funziona|serve|fa bene|fa male)\b",
        r"\b(?:cura|guarisce|previene|elimina|disintossica|detox)\w*\b",
        r"\b(?:tessuti|cellule|organismo)\b.{0,45}\b(?:assimila|assorbe|regola)\w*\b",
        r"\b(?:attiva|stimola|riequilibra)\s+(?:il|gli|la)\s+(?:nervo|ormoni|metabolismo)\b",
    )
    if any(re.search(pattern, combined) for pattern in risky_patterns):
        raise ValueError("La storia contiene una spiegazione fisiologica troppo categorica")
    return fields


def generate_story_image() -> tuple[Path, str, str]:
    console.print("[cyan]1. Generazione testi per la Storia via AGY...[/]")

    ebooks = load_ebook_catalog()
    ebook_data = random.choices(
        ebooks,
        weights=[float(book["socialWeight"]) for book in ebooks],
        k=1,
    )[0]
    ebook = ebook_data["title"]
    delivery_note = (
        "un PDF gratuito inviato via email"
        if ebook_data["deliveryType"] == "pdf_email"
        else "un'anteprima gratuita leggibile subito online"
    )

    prompt = f"""
Sei un editor esperto di benessere per ConsciaMente. Crea UNA Storia Instagram utile e concreta sul tema '{ebook}'.
La risorsa collegata è {delivery_note}; non descriverla in modo diverso.
Non scrivere una frase motivazionale generica: insegna una piccola idea applicabile oggi.
La storia deve avere una progressione completa:
1. "titolo": hook specifico da 4 a 9 parole, senza allarmismo.
2. "insight": spiegazione divulgativa da 18 a 38 parole, chiara e prudente; descrivi un'abitudine o uno spunto di osservazione, non meccanismi fisiologici e non superiorità assolute.
3. "azione": esercizio o domanda pratica da 7 a 18 parole, flessibile e senza quantità/orari rigidi.
Usa formule prudenti come "può aiutarti a osservare". Non affermare che una pratica cura, previene, attiva organi/nervi, regola ormoni o viene assimilata meglio.
Non inserire URL, hashtag, richieste di DM o la CTA finale: viene aggiunta graficamente dal sistema.

Restituisci SOLO un file JSON valido:
{{
  "titolo": "...",
  "insight": "...",
  "azione": "..."
}}
"""
    last_error = "risposta assente"
    for attempt in range(1, 4):
        raw_json = call_agy(prompt)
        match = re.search(r'\{.*\}', raw_json, re.DOTALL)
        try:
            if match is None:
                raise ValueError("JSON assente")
            story = validate_story_content(json.loads(match.group(0)))
            titolo, insight, azione = story["titolo"], story["insight"], story["azione"]
            break
        except (json.JSONDecodeError, KeyError, AttributeError, ValueError) as exc:
            last_error = str(exc)
            console.print(f"[yellow]Testo storia non valido ({attempt}/3): {last_error}[/]")
    else:
        raise RuntimeError(f"AGY non ha prodotto una storia valida: {last_error}")

    console.print(f"[bold]Titolo:[/] {titolo}\n[bold]Insight:[/] {insight}\n[bold]Azione:[/] {azione}")

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
        font_body = ImageFont.truetype(str(font_path), 43)
        font_action = ImageFont.truetype(str(font_path), 39)
        font_cta = ImageFont.truetype(str(font_path), 38)
    except:
        font_titolo = ImageFont.load_default()
        font_body = ImageFont.load_default()
        font_action = ImageFont.load_default()
        font_cta = ImageFont.load_default()

    # Disegna Titolo (Centro alto)
    lines_titolo = wrap_text(titolo, font_titolo, 850, draw)
    y_text = 330
    for line in lines_titolo:
        # Usa l'ancoraggio centrale 'mm' per il bounding box e il disegno
        bbox = draw.textbbox((1080/2, y_text), line, font=font_titolo, anchor="mm")

        # Ombra
        draw.text((1080/2 + 4, y_text + 4), line, font=font_titolo, fill="black", anchor="mm")
        # Testo
        draw.text((1080/2, y_text), line, font=font_titolo, fill="white", anchor="mm")

        y_text += (bbox[3] - bbox[1]) + 25

    # Insight centrale: abbastanza ricco da offrire valore, ma leggibile.
    y_body = 690
    for line in wrap_text(insight, font_body, 840, draw):
        draw.text((540, y_body), line, font=font_body, fill=(240, 240, 245), anchor="mm")
        y_body += 62

    # Azione pratica in una card distinta.
    action_lines = wrap_text(azione, font_action, 760, draw)
    card_top, card_bottom = 1040, 1040 + max(190, len(action_lines) * 60 + 90)
    draw.rounded_rectangle([105, card_top, 975, card_bottom], radius=32, fill=(42, 34, 65, 225), outline=(168, 85, 247), width=3)
    draw.text((540, card_top + 48), "PROVA OGGI", font=font_cta, fill=(196, 181, 253), anchor="mm")
    y_action = card_top + 112
    for line in action_lines:
        draw.text((540, y_action), line, font=font_action, fill="white", anchor="mm")
        y_action += 58

    # CTA realmente eseguibile: il collegamento cliccabile è nella bio.
    draw.rounded_rectangle([155, 1480, 925, 1605], radius=62, fill=(74, 222, 128, 245))
    draw.text((540, 1542), ebook_data["storyCta"], font=font_cta, fill=(2, 44, 34), anchor="mm")
    draw.text((540, 1680), "ConsciaMente", font=font_cta, fill=(220, 220, 230), anchor="mm")

    img.save(out_path, quality=90)
    console.print(f"[green]Immagine storia salvata in: {out_path}[/]")
    return out_path, ebook_data["id"], f"{titolo} {insight} {azione}"

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
    image_path, ebook_id, story_text = generate_story_image()
    if args.no_publish:
        console.print("[yellow]Modalità --no-publish: storia generata senza upload.[/]")
        return
    ebook_data = next(book for book in load_ebook_catalog() if book["id"] == ebook_id)
    campaign = create_campaign("instagram", f"{ebook_data['title']} {story_text}", "story")
    save_campaign(campaign)
    with TemporaryMediaServer([image_path]) as media_server:
        if not publish_story(image_path, media_server.url_for(image_path)):
            raise SystemExit(1)
    log_upload(
        video_file=str(image_path), hook_title=story_text.split(".", 1)[0],
        category=ebook_data["category"], mode="story", quality_score=8, fonte="", success=True,
        topic=ebook_data["title"], platform="instagram", resource_id=ebook_id,
        delivery_type=ebook_data["deliveryType"],
    )

if __name__ == "__main__":
    main()
