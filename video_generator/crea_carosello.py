import os
import sys
import json
import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from google import genai
from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip
import subprocess
from rich.console import Console
from dotenv import load_dotenv

import config

load_dotenv()
console = Console()

def genera_testi_carosello():
    slides_file = config.SCRIPTS_DIR / "slides_carosello.json"
    if slides_file.exists():
        try:
            slides = json.loads(slides_file.read_text(encoding="utf-8"))
            if isinstance(slides, list) and len(slides) >= 5:
                # Se è un dict estrai 'text' o 'testo'
                pulite = []
                import re
                import ast
                for s in slides[:6]:
                    testo = str(s)
                    if isinstance(s, dict):
                        testo = s.get('overlay_text', s.get('testo_schermo', s.get('text', s.get('testo', s.get('content', s.get('testo_principale', s.get('text_on_screen', str(s))))))))
                    elif isinstance(s, str):
                        try:
                            parsed = json.loads(s)
                            if isinstance(parsed, dict):
                                testo = parsed.get('overlay_text', parsed.get('testo_schermo', parsed.get('text', parsed.get('testo', parsed.get('content', parsed.get('testo_principale', parsed.get('text_on_screen', s)))))))
                        except:
                            try:
                                parsed = ast.literal_eval(s)
                                if isinstance(parsed, dict):
                                    testo = parsed.get('overlay_text', parsed.get('testo_schermo', parsed.get('text', parsed.get('testo', parsed.get('content', parsed.get('testo_principale', parsed.get('text_on_screen', s)))))))
                            except:
                                pass

                    if not isinstance(testo, str):
                        testo = str(testo)

                    # Rimuovi eventuali tag HTML e markdown residui
                    testo = re.sub(r'<[^>]+>', '', testo)
                    testo = re.sub(r'\*\*(.*?)\*\*', r'\1', testo)
                    testo = re.sub(r'\*(.*?)\*', r'\1', testo)

                    pulite.append(testo)

                # Sovrascriviamo il json ripulito per Playwright
                slides_file.write_text(json.dumps(pulite, ensure_ascii=False), encoding='utf-8')
                return pulite
        except Exception as e:
            console.print(f"[red]Errore lettura slides_carosello.json:[/] {e}")

    # Fallback se il file non esiste o ha errori
    return [
        "Pensi di non avere tempo per meditare?",
        "La verità è che basta iniziare con piccoli passi.",
        "La consapevolezza si allena nelle piccole pause.",
        "Oggi fai 3 respiri profondi prima di aprire il telefono.",
        "Salva questo post per ricordartelo! 🤍"
    ]


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

def crea_immagini(slides, output_dir):
    console.print("[cyan]Rendering HTML Slides via Playwright...[/]")
    cmd = [sys.executable, "genera_immagini_carosello.py", str(config.SCRIPTS_DIR / "slides_carosello.json")]
    subprocess.run(cmd, check=True)

    image_paths = []
    # I file sono salvati in assets/carousels/ come slide_X.png
    assets_dir = Path("assets/carousels")
    for i in range(1, len(slides) + 1):
        png_path = assets_dir / f"slide_{i}.png"
        jpg_path = output_dir / f"slide_{i}.jpg"
        if png_path.exists():
            img = Image.open(png_path).convert("RGB")
            img.save(jpg_path)
            image_paths.append(jpg_path)
    return image_paths

def main(no_publish: bool = False):
    console.print("[cyan]1. Generazione Testi per il Carosello...[/]")
    slides = genera_testi_carosello()

    console.print("[cyan]2. Creazione Immagini...[/]")
    temp_dir = config.TEMP_DIR / "carousel"
    temp_dir.mkdir(parents=True, exist_ok=True)
    image_paths = crea_immagini(slides, temp_dir)

    console.print("[cyan]3. Creazione Video MP4 dalle immagini e aggiunta musica...[/]")
    clips = []
    for p in image_paths:
        clip = ImageClip(str(p)).set_duration(11.0)
        clips.append(clip)

    final_video = concatenate_videoclips(clips, method="compose")

    # Aggiunta Audio
    audio_files = list(config.AMBIENT_DIR.glob("*.mp3")) + list(config.AMBIENT_DIR.glob("*.wav"))
    if audio_files:
        chosen_audio = random.choice(audio_files)
        console.print(f"[cyan]Aggiungo traccia audio: {chosen_audio.name}[/]")
        audio_clip = AudioFileClip(str(chosen_audio))

        # Loop audio if it's shorter than the video, or cut if it's longer
        if audio_clip.duration < final_video.duration:
            import math
            n_loops = math.ceil(final_video.duration / audio_clip.duration)
            from moviepy.editor import concatenate_audioclips
            audio_clip = concatenate_audioclips([audio_clip] * n_loops)

        audio_clip = audio_clip.subclip(0, final_video.duration)
        final_video = final_video.set_audio(audio_clip)
    else:
        console.print("[yellow]Nessun file audio trovato in assets/ambient. Genero video muto.[/]")

    out_video = config.OUTPUT_DIR / "carosello_finale.mp4"
    final_video.write_videofile(
        str(out_video),
        fps=30,
        codec="libx264",
        audio_codec="aac" if audio_files else None,
        logger=None
    )

    # Salva il testo della prima slide come "script" così step4 lo legge per generare descrizione
    script_path = config.SCRIPTS_DIR / "script_carosello.txt"
    script_path.write_text(" ".join(slides), encoding="utf-8")

    if no_publish:
        console.print("[bold yellow]Modalità --no-publish: carosello generato senza upload.[/]")
        return

    console.print("[cyan]4. Pubblicazione su TikTok...[/]")
    import sys

    tiktok_caption_path = config.SCRIPTS_DIR / "tiktok_caption.txt"
    mode = "virale"
    if tiktok_caption_path.exists():
        caption_text = tiktok_caption_path.read_text(encoding="utf-8").lower()
        if "link in bio" in caption_text or "amazon" in caption_text:
            mode = "promo"

    # Esegue step4_pubblica.py (per TikTok/YouTube)
    cmd = [
        sys.executable, "step4_pubblica.py",
        "--video", str(out_video),
        "--script", str(script_path),
        "--mode", mode
    ]
    subprocess.run(cmd, check=True)

    console.print("[cyan]5. Pubblicazione su Instagram (Carosello Nativo Foto)...[/cyan]")
    cmd_ig = [
        sys.executable, "step4_pubblica_ig_carousel_api.py",
        "--dir", str(temp_dir)
    ]
    ig_result = subprocess.run(cmd_ig, check=False)
    if ig_result.returncode != 0:
        console.print(
            "[bold red]TikTok completato, ma la pubblicazione Instagram è fallita. "
            "Il job termina con errore per non produrre un falso successo.[/]"
        )
        raise SystemExit(ig_result.returncode)

    console.print("[bold green]Tutte le pubblicazioni (Video e Carosello Foto) completate![/bold green]")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-publish", action="store_true")
    main(no_publish=parser.parse_args().no_publish)
