import sys
import os
import time
import random
import argparse
import subprocess
import json
from pathlib import Path
from rich.console import Console
from modules.email_notifications import notify_email

console = Console()

def chiama_agy(prompt: str) -> str:
    try:
        result = subprocess.run(
            ['agy', '--dangerously-skip-permissions', '--print', prompt],
            text=True, capture_output=True, timeout=120, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        raise RuntimeError(f"Errore AGY: {e}") from e

def estrai_json(testo: str) -> str:
    import re
    match = re.search(r'```json\s*([\s\S]*?)\s*```', testo)
    if match: return match.group(1)
    match_array = re.search(r'\[\s*\{.*\}\s*\]', testo, re.DOTALL)
    if match_array: return match_array.group(0)
    match_obj = re.search(r'\{\s*".*\}\s*', testo, re.DOTALL)
    if match_obj: return match_obj.group(0)
    return testo

def valida_contenuto(dati: object) -> dict:
    if isinstance(dati, list):
        dati = {"slides": dati, "caption": "Scopri Conscia-Mente."}
    if not isinstance(dati, dict):
        raise ValueError("AGY non ha restituito un oggetto JSON")

    slides = dati.get("slides")
    caption = dati.get("caption")
    if not isinstance(slides, list) or not 5 <= len(slides) <= 6:
        raise ValueError("Il contenuto deve avere da 5 a 6 slide")
    if not isinstance(caption, str) or not caption.strip():
        raise ValueError("Caption mancante")

    normalized_slides = []
    for index, slide in enumerate(slides, start=1):
        text = slide.get("overlay_text") if isinstance(slide, dict) else slide
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Testo mancante nella slide {index}")
        if len(text.split()) > 20:
            raise ValueError(f"La slide {index} supera il limite di 20 parole")
        normalized_slides.append({"slide_number": index, "overlay_text": text.strip()})
    return {"slides": normalized_slides, "caption": caption.strip()}


def genera_contenuto(prodotto: str, piattaforma: str) -> dict:
    prompt_base = f"""Sei un copywriter esoterico e spirituale per il sito 'Conscia-Mente'.
Devi creare un carosello video (5-6 slide) per {piattaforma} per promuovere il prodotto: {prodotto.upper()}.

Regole per il Copy:
- Se il prodotto è ORACOLO: promuovilo NON in modo troppo aereo o spirituale, ma presentalo come una "Chat IA gratuita per il tuo benessere e miglioramento personale".
- Se il prodotto è NUMEROLOGIA: integra e promuovi il fatto che offriamo un "Check per l'affinità di coppia" e un "Profilo individuale completo con Numeri del Destino, dell'Anima, della Personalità e della Maturità".
- Spiega brevemente un concetto utile legato al {prodotto}, poi aggancialo immediatamente ai nostri strumenti gratuiti.
- Tono: Motivazionale, pratico e orientato al miglioramento personale (meno esoterico e più incentrato sul benessere concreto).
- Nelle slide, l'utente leggerà SOLO 'overlay_text' (massimo 15-20 parole per slide).
- NELL'ULTIMA SLIDE fai la Call to Action forte: invita l'utente a provare i nostri strumenti gratuiti legati a {prodotto} sul sito Conscia-Mente.
- Per Instagram e TikTok usa una CTA realmente eseguibile: "Prova gratis dal link in bio". Non promettere risposte o invii automatici nei DM.

Restituisci ESATTAMENTE questo JSON:
{{
  "slides": [
    {{"slide_number": 1, "overlay_text": "..."}},
    ...
  ],
  "caption": "Caption intrigante con hashtag rilevanti."
}}
"""
    raw_output = chiama_agy(prompt_base)
    pulito = estrai_json(raw_output)
    try:
        return valida_contenuto(json.loads(pulito))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Output AGY non valido: {exc}") from exc

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prodotto", choices=["oracolo", "numerologia"], required=True)
    parser.add_argument("--piattaforma", choices=["tiktok", "ig"], required=True)
    parser.add_argument("--no-publish", action="store_true", help="Genera gli asset senza caricarli sui social")
    args = parser.parse_args()

    os.chdir(Path(__file__).parent)

    # 1. Genera Contenuto
    console.print(f"[cyan]Generazione contenuto {args.prodotto} per {args.piattaforma}...[/]")
    dati = genera_contenuto(args.prodotto, args.piattaforma)
    # 2. Salva temporaneamente per script successivi
    testi = [s.get("overlay_text", "") for s in dati["slides"]]
    with open("scripts/slides_carosello.json", "w", encoding="utf-8") as f:
        json.dump(testi, f, ensure_ascii=False)
    with open("scripts/ig_caption.txt", "w", encoding="utf-8") as f:
        f.write(dati["caption"])
    with open("scripts/script_carosello.txt", "w", encoding="utf-8") as f:
        f.write(" ".join(testi))

    # Per evitare l'upload cross-platform di crea_carosello.py, chiamiamo solo i task di creazione.
    console.print("[cyan]Creazione immagini (usando cartella locale cosciamente se presente)...[/]")
    subprocess.run([
        sys.executable,
        "genera_immagini_carosello.py",
        "scripts/slides_carosello.json",
        "dark",
        "/home/ubuntu/home/cosciamente"
    ], check=True)

    console.print("[cyan]Creazione Video MP4...[/]")
    # Usiamo moviepy qui invece di usare crea_carosello.py per isolare la piattaforma
    from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip
    image_paths = sorted(list(Path("assets/carousels").glob("slide_*.png")))
    clips = [ImageClip(str(p)).set_duration(9.0) for p in image_paths]
    final_video = concatenate_videoclips(clips, method="compose")

    # Audio
    audio_files = list(Path("assets/ambient").glob("*.mp3"))
    if audio_files:
        audio = AudioFileClip(str(random.choice(audio_files)))
        import math
        from moviepy.editor import concatenate_audioclips
        if audio.duration < final_video.duration:
            audio = concatenate_audioclips([audio] * math.ceil(final_video.duration / audio.duration))
        final_video = final_video.set_audio(audio.subclip(0, final_video.duration))

    out_video = Path("output") / f"{args.prodotto}_{args.piattaforma}.mp4"
    final_video.write_videofile(str(out_video), fps=30, codec="libx264", audio_codec="aac" if audio_files else None, logger=None)

    # 3. Pubblicazione Mirata
    if args.no_publish:
        console.print("[bold yellow]Modalità --no-publish: upload social disabilitato.[/]")
        return

    console.print(f"[cyan]Pubblicazione su {args.piattaforma}...[/]")
    if args.piattaforma == "tiktok":
        subprocess.run([sys.executable, "step4_pubblica.py", "--video", str(out_video), "--script", "scripts/script_carosello.txt", "--mode", "virale"], check=True)
    elif args.piattaforma == "ig":
        # Pubblicazione IG nativa come Reel video
        console.print("[cyan]Caricamento come Reel Video su IG...[/]")
        subprocess.run([sys.executable, "step4_pubblica_ig_api.py", "--video", str(out_video), "--script", "scripts/script_carosello.txt", "--mode", "virale"], check=True)

    notify_email(f"Esito: PUBBLICATO\nFormato: contenuto {args.prodotto}\nCanale: {args.piattaforma}\nFile: {out_video.name}")
    console.print(f"[bold green]Operazione completata per {args.prodotto} su {args.piattaforma}[/]")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        notify_email(f"ERRORE job social Conscia-Mente: {type(exc).__name__}: {exc}")
        raise
