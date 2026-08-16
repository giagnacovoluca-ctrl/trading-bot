"""Apre Chromium su display :99 per fare login TikTok e salvare il profilo."""
import os, time
os.environ["DISPLAY"] = ":99"

from playwright.sync_api import sync_playwright

PROFILE_DIR = "/home/ubuntu/GIT/video_generator/chrome_profile"
os.makedirs(PROFILE_DIR, exist_ok=True)

print("Avvio Chromium con profilo persistente su display :99...")
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--start-maximized",
            "--window-size=1200,850",
        ],
        viewport={"width": 1200, "height": 850},
        ignore_https_errors=True,
    )
    page = ctx.new_page()
    page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")
    print("Chromium aperto su TikTok login!")
    print("Connettiti al VNC: http://141.94.79.16:6080/vnc.html")
    print("Fai login su TikTok.")
    print("(Attesa max 15 minuti...)")

    # Aspetta che l'utente faccia login — URL cambia da /login
    try:
        page.wait_for_url(lambda url: "login" not in url, timeout=900000)
        print("Login rilevato! Attendo 5 secondi per salvare i cookie...")
        time.sleep(5)
    except Exception:
        print("Timeout — salvo comunque il profilo attuale.")

    ctx.close()
    print("Profilo salvato in:", PROFILE_DIR)
