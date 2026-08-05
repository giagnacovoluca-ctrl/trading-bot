import asyncio
from playwright.async_api import async_playwright
import sys

async def check_login():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        
        # Load cookies
        cookies = []
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
        
        page = await context.new_page()
        await page.goto("https://www.tiktok.com/upload?lang=en")
        await page.wait_for_timeout(5000)
        
        # Take a screenshot to see if we are logged in or blocked
        await page.screenshot(path="tiktok_debug.png")
        print("Screenshot saved to tiktok_debug.png")
        await browser.close()

asyncio.run(check_login())
