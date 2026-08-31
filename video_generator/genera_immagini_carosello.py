import sys
import json
import asyncio
import os
import random
from pathlib import Path
from playwright.async_api import async_playwright

async def generate_carousel_images(json_path: str, mode: str = 'default', bg_dir: str = None):
    p_file = Path(json_path)
    if not p_file.exists():
        print(f"File {json_path} non trovato!")
        sys.exit(1)

    with open(p_file, "r", encoding="utf-8") as f:
        try:
            slides = json.load(f)
        except json.JSONDecodeError:
            print("Errore parsing JSON.")
            sys.exit(1)

    if not isinstance(slides, list):
        print("Il JSON deve essere un array di stringhe.")
        sys.exit(1)

    out_dir = Path("assets/carousels")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Pulizia vecchia generazione
    for old_file in out_dir.glob("slide_*.png"):
        old_file.unlink()
    for old_file in out_dir.glob("bg_slide_*.jpg"):
        old_file.unlink()

    # Template selection based on mode
    _mode = mode.lower().strip()
    if _mode == 'promo':
        _template_name = "carousel_promo.html"
    elif _mode in ('dark', 'bastian'):
        _template_name = "carousel_dark.html"
    else:
        _template_name = "carousel_slide.html"
    template_path = Path(f"templates/{_template_name}").resolve()
    print(f"Template selezionato: {_template_name} (mode='{mode}')")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # 1080x1920 per TikTok / Reels
        page = await browser.new_page(viewport={"width": 1080, "height": 1920})

        # Load local HTML file
        await page.goto(f"file://{template_path}")

        # Attesa per il caricamento del font
        await page.wait_for_timeout(1000)

        # Seleziona un tema casuale per l'intero carosello (da 0 a 4)
        theme_index = random.randint(0, 4)

        for i, slide_text in enumerate(slides):
            current = i + 1
            total = len(slides)

            # Escape text for JS
            safe_text = json.dumps(slide_text)

            import urllib.parse
            import urllib.request
            import time
            import subprocess
            from PIL import Image, ImageDraw

            prompt = f"Abstract beautiful background for {slide_text[:50]}, dark mode, minimalist, elegant, 9:16 vertical"
            encoded_prompt = urllib.parse.quote(prompt)
            bg_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1920&model=flux&enhance=true&nologo=true&private=true&seed={int(time.time())+i}"

            local_bg_path = out_dir / f"bg_slide_{current}.jpg"

            # Ogni slide deve nascere in questo run. Un file residuo farebbe
            # sembrare riuscito un fallback fallito, mescolando caroselli diversi.
            local_bg_path.unlink(missing_ok=True)

            success = False

            # Se è stata passata una cartella di sfondi custom, usala!
            if bg_dir and Path(bg_dir).exists():
                custom_bgs = list(Path(bg_dir).glob("*.jpg")) + list(Path(bg_dir).glob("*.png")) + list(Path(bg_dir).glob("*.jpeg"))
                if custom_bgs:
                    import shutil
                    chosen_bg = random.choice(custom_bgs)
                    shutil.copy(str(chosen_bg), str(local_bg_path))
                    success = True
                    print(f"Uso sfondo custom locale: {chosen_bg.name}")

            if not success:
                for attempt, delay in enumerate([2, 4, 6]):
                    try:
                        req = urllib.request.Request(bg_url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req, timeout=10) as response, open(local_bg_path, 'wb') as out_file:
                            out_file.write(response.read())
                        success = True
                        break
                    except Exception as e:
                        print(f"Tentativo {attempt+1} fallito per Pollinations: {e}")
                        time.sleep(delay)

            if not success:
                print(f"Pollinations fallito per slide {current}, avvio fallback Antigravity...")
                fallback_prompt = f"Sei l'agente di backup. Il motore primario ha fallito. DEVI usare il tuo tool interno 'generate_image' per generare uno sfondo verticale (AspectRatio 9:16) astratto per: '{slide_text[:50]}'. NESSUN TESTO. Dopo averla generata, usa 'run_command' per copiarla fisicamente in: {local_bg_path.resolve()}. Rispondi solo 'OK'."
                subprocess.run(["agy", "--dangerously-skip-permissions", "--print", fallback_prompt], capture_output=True)

            if not local_bg_path.exists():
                print(f"Fallback AGY fallito o file non trovato. Genero sfondo gradient PIL per slide {current}.")
                # theme-0=giallo, theme-1=verde, theme-2=rosso, theme-3=blu, theme-4=arancio
                themes = [
                    ((30, 30, 0), (150, 150, 0)),    # Giallo scuro -> chiaro
                    ((0, 30, 0), (0, 150, 0)),      # Verde scuro -> chiaro
                    ((30, 0, 0), (150, 0, 0)),      # Rosso scuro -> chiaro
                    ((0, 0, 30), (0, 0, 150)),      # Blu scuro -> chiaro
                    ((40, 20, 0), (180, 90, 0)),    # Arancio scuro -> chiaro
                ]
                color1, color2 = themes[theme_index]
                # Modifica leggermente i colori in base a 'current' per variare le slide
                c1_r = min(255, color1[0] + current * 5)
                c1_g = min(255, color1[1] + current * 5)
                c1_b = min(255, color1[2] + current * 5)
                c2_r = min(255, color2[0] + current * 10)
                c2_g = min(255, color2[1] + current * 10)
                c2_b = min(255, color2[2] + current * 10)

                img = Image.new('RGB', (1080, 1920), (c1_r, c1_g, c1_b))
                draw = ImageDraw.Draw(img)
                for y in range(1920):
                    r = int(c1_r + (c2_r - c1_r) * y / 1920)
                    g = int(c1_g + (c2_g - c1_g) * y / 1920)
                    b = int(c1_b + (c2_b - c1_b) * y / 1920)
                    draw.line([(0, y), (1080, y)], fill=(r, g, b))
                img.save(local_bg_path)

            # Passa il percorso locale (assoluto) all'HTML del carosello
            local_bg_url = f"file://{local_bg_path.resolve()}"
            await page.evaluate(f"setSlideData({safe_text}, {current}, {total}, {theme_index}, '{local_bg_url}')")

            # Scatta screenshot
            out_path = out_dir / f"slide_{current}.png"
            await page.screenshot(path=str(out_path))
            print(f"Screenshot salvato: {out_path}")

        await browser.close()

    print(f"Generazione {len(slides)} slide completata con successo.")

if __name__ == "__main__":
    json_path = sys.argv[1] if len(sys.argv) > 1 else "scripts/slides_carosello.json"
    mode = sys.argv[2] if len(sys.argv) > 2 else "default"
    bg_dir = sys.argv[3] if len(sys.argv) > 3 else None
    asyncio.run(generate_carousel_images(json_path, mode, bg_dir))
