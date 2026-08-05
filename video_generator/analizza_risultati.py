import asyncio
import argparse
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright

async def get_views(username, limit=10):
    async with async_playwright() as p:
        # Launch browser
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        
        # Load cookies
        cookies = []
        try:
            with open("cookies.txt", "r") as f:
                for line in f:
                    if line.startswith("#") or not line.strip(): continue
                    parts = line.strip().split("\t")
                    if len(parts) >= 7:
                        cookies.append({
                            "domain": parts[0],
                            "path": parts[2],
                            "secure": parts[3] == "TRUE",
                            "expires": int(parts[4]),
                            "name": parts[5],
                            "value": parts[6]
                        })
            await context.add_cookies(cookies)
            print("Cookie caricati con successo.")
        except Exception as e:
            print(f"Avviso: Errore nel caricamento dei cookie (file 'cookies.txt'): {e}")
            
        page = await context.new_page()
        profile_url = f"https://www.tiktok.com/@{username}"
        print(f"Navigando su {profile_url} ...")
        
        try:
            # Go to profile
            await page.goto(profile_url, timeout=60000)
            
            # Wait for views selector or timeout
            try:
                await page.wait_for_selector('strong[data-e2e="video-views"]', timeout=15000)
            except Exception as e:
                print(f"Avviso: Timeout in attesa del selettore delle visualizzazioni. La pagina potrebbe aver bloccato la richiesta o non ci sono video: {e}")
                
            # Wait a bit for everything to stabilize
            await page.wait_for_timeout(3000)
            
            # Extract views
            view_elements = await page.query_selector_all('strong[data-e2e="video-views"]')
            
            views_data = []
            for i, el in enumerate(view_elements):
                if i >= limit:
                    break
                text = await el.inner_text()
                views_data.append(text.strip())
                
            print(f"Visualizzazioni trovate per gli ultimi {len(views_data)} video: {views_data}")
            
            # Save to JSON
            out_dir = Path("scripts")
            out_dir.mkdir(exist_ok=True, parents=True)
            out_file = out_dir / "analytics.json"
            
            data_to_save = {
                "username": username,
                "latest_views": views_data
            }
            
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(data_to_save, f, indent=4)
            print(f"Risultati salvati in {out_file}")
            
        except Exception as e:
            print(f"Errore durante l'analisi del profilo: {e}")
        finally:
            await browser.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analizza le views degli ultimi video di un profilo TikTok")
    parser.add_argument("--user", type=str, required=True, help="Username di TikTok (senza @)")
    parser.add_argument("--limit", type=int, default=10, help="Numero massimo di video da analizzare")
    args = parser.parse_args()
    
    asyncio.run(get_views(args.user, args.limit))
