import os
import sys
import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright
import subprocess
import config

def create_aesthetic_reel(text: str, category: str, output_path: str, cta: str = ""):
    html_path = Path("templates/ig_aesthetic.html").absolute()
    screenshot_path = Path("output/ig_temp.png").absolute()

    # 1. Genera l'immagine con Playwright
    print(f"[*] Generazione immagine (Categoria: {category})...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1080, "height": 1920})
        page.goto(f"file://{html_path}")

        # Passa i dati come argomenti serializzati, senza costruire JavaScript.
        page.evaluate("([value, kind, action]) => setContent(value, kind, action)", [text, category, cta])

        # Attendi il rendering e scatta
        page.wait_for_timeout(500)
        page.screenshot(path=str(screenshot_path))
        browser.close()

    print("[*] Immagine generata. Applicazione magia FFMPEG (Slow Zoom + Audio)...")

    # 2. Crea il video con ffmpeg (Zoom lento + Audio)
    # L'effetto Ken Burns: scala da 1.0 a 1.1 in 6 secondi
    import random

    # Seleziona la cartella musicale in base alla categoria
    audio_base = Path("assets/ambient").absolute()
    if category in ['nervo_vago', 'meditazione', 'acqua_idratazione']:
        audio_dir = audio_base / "zen"
    elif category in ['epigenetica']:
        audio_dir = audio_base / "scienza"
    elif category in ['cibo_salute', 'integratori']:
        audio_dir = audio_base / "energica"
    else:
        audio_dir = audio_base

    audio_files = list(audio_dir.rglob("*.mp3"))
    if not audio_files:
        audio_files = list(audio_base.rglob("*.mp3")) # Fallback a tutti

    audio_path = random.choice(audio_files) if audio_files else audio_base / "lofi1.mp3"

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [config._FFMPEG_BIN, "-y", "-loop", "1", "-i", str(screenshot_path)]
    if audio_path.exists():
        cmd.extend(["-i", str(audio_path)])
    cmd.extend([
        "-vf", "zoompan=z='min(zoom+0.0005,1.1)':d=180:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920,fps=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "6", "-map", "0:v",
    ])
    if audio_path.exists():
        cmd.extend(["-map", "1:a", "-c:a", "aac", "-shortest"])
    cmd.append(str(output))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"[+] Reel Aesthetic creato con successo in: {output_path}")

    # Pulisci file temporaneo
    if screenshot_path.exists():
        screenshot_path.unlink()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Genera un Reel estetico testuale")
    parser.add_argument("--text", type=str, required=True, help="Testo da mostrare a schermo")
    parser.add_argument("--category", type=str, default="default", help="Categoria per il colore (es: cibo_salute, nervo_vago)")
    parser.add_argument("--out", type=str, default="output/aesthetic_reel.mp4", help="Percorso di output")
    parser.add_argument("--cta", type=str, default="", help="CTA breve da mostrare nel Reel")

    args = parser.parse_args()

    create_aesthetic_reel(args.text, args.category, args.out, args.cta)
