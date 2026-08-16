import asyncio
from playwright.async_api import async_playwright

async def check_profile():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        
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
        await page.goto("https://www.tiktok.com/upload")
        await page.wait_for_timeout(5000)
        await page.screenshot(path="tiktok_upload_check.png")
        print("Screenshot saved to tiktok_upload_check.png")
        
        await page.goto("https://www.tiktok.com/following")
        await page.wait_for_timeout(5000)
        await page.screenshot(path="tiktok_following_check.png")
        print("Screenshot saved to tiktok_following_check.png")

        await browser.close()

asyncio.run(check_profile())
