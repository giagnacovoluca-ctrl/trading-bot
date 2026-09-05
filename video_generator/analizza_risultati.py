import asyncio
import argparse
import json
import os
import re
from pathlib import Path
from playwright.async_api import async_playwright
from modules.feedback_loop import update_recent_tiktok_views

ROOT = Path(__file__).resolve().parent


def parse_view_count(value: str) -> int:
    normalized = value.strip().upper().replace(",", ".")
    match = re.match(r"([0-9]+(?:\.[0-9]+)?)([KMB]?)", normalized)
    if not match:
        return 0
    number = float(match.group(1))
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2)]
    return int(number * multiplier)

async def get_views(username, limit=10, profile_dir="chrome_profile"):
    async with async_playwright() as p:
        profile_path = Path(profile_dir)
        if not profile_path.is_absolute():
            profile_path = ROOT / profile_path
        browser = None
        if profile_path.exists():
            context = await p.chromium.launch_persistent_context(
                str(profile_path.resolve()),
                headless=True,
                executable_path='/usr/bin/google-chrome',
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-dev-shm-usage'],
                ignore_default_args=['--enable-automation'],
                viewport={"width": 1280, "height": 900},
            )
            print(f"Profilo TikTok persistente caricato: {profile_path}")
        else:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
        
        # Load cookies
        cookies = []
        try:
            with open(ROOT / "cookies.txt", "r") as f:
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
            if cookies and not profile_path.exists():
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
            numeric_views = [parse_view_count(value) for value in views_data]
            if not numeric_views:
                await page.screenshot(path=str(ROOT / 'output/tiktok_analytics_diagnostic.png'))
                print('Nessuna metrica rilevata: snapshot diagnostico salvato, nessuno zero attribuito ai post.')
            # La posizione nella griglia non identifica un post (pinned/rimossi).
            # Conserva lo snapshot senza inventare associazioni cronologiche.
            from modules.feedback_loop import update_tiktok_metrics
            metrics = {}
            for el in view_elements[:limit]:
                href = await el.evaluate("el => el.closest('a')?.href || ''")
                match = re.search(r'/(?:video|photo)/(\d+)', href)
                if match:
                    metrics[match.group(1)] = parse_view_count(await el.inner_text())
            updated = update_tiktok_metrics(metrics)
            print(f"Metriche collegate a {updated} pubblicazioni TikTok tracciate.")
            
            # Save to JSON
            out_dir = ROOT / "scripts"
            out_dir.mkdir(exist_ok=True, parents=True)
            out_file = out_dir / "analytics.json"
            
            data_to_save = {
                "username": username,
                "latest_views": views_data
                ,"latest_views_numeric": numeric_views,
                "metrics_by_post_id": metrics,
                "status": "ok" if metrics else "no_metrics",
                "matched_uploads": updated,
            }
            
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(data_to_save, f, indent=4)
            print(f"Risultati salvati in {out_file}")
            
        except Exception as e:
            print(f"Errore durante l'analisi del profilo: {e}")
        finally:
            await context.close()
            if browser:
                await browser.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analizza le views degli ultimi video di un profilo TikTok")
    parser.add_argument("--user", type=str, required=True, help="Username di TikTok (senza @)")
    parser.add_argument("--limit", type=int, default=10, help="Numero massimo di video da analizzare")
    parser.add_argument("--profile-dir", default="chrome_profile", help="Profilo browser persistente TikTok")
    parser.add_argument("--skip-instagram", action="store_true", help="Non sincronizzare gli insight Instagram")
    args = parser.parse_args()
    
    asyncio.run(get_views(args.user, args.limit, args.profile_dir))
    if not args.skip_instagram:
        try:
            from sync_instagram_insights import sync_insights
            linked, updated = sync_insights()
            print(f"Instagram: {linked} ID storici collegati, {updated} Reel aggiornati con insight Meta.")
        except Exception as exc:
            # Un errore Meta non deve impedire l'analisi TikTok del cron.
            print(f"Avviso: sincronizzazione insight Instagram non riuscita ({type(exc).__name__}).")
