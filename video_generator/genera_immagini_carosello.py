import sys
import json
import asyncio
import os
import random
from pathlib import Path
from playwright.async_api import async_playwright

async def generate_carousel_images(json_path: str):
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

    template_path = Path("templates/carousel_slide.html").resolve()
    
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
            
            await page.evaluate(f"setSlideData({safe_text}, {current}, {total}, {theme_index})")
            
            # Scatta screenshot
            out_path = out_dir / f"slide_{current}.png"
            await page.screenshot(path=str(out_path))
            print(f"Screenshot salvato: {out_path}")
            
        await browser.close()
        
    print(f"Generazione {len(slides)} slide completata con successo.")

if __name__ == "__main__":
    json_path = sys.argv[1] if len(sys.argv) > 1 else "scripts/slides_carosello.json"
    asyncio.run(generate_carousel_images(json_path))
