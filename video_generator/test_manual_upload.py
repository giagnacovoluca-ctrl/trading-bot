import asyncio
from playwright.async_api import async_playwright

async def upload_test():
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
        
        # Upload video
        file_input = page.locator("input[type='file']")
        await file_input.set_input_files("temp_1.mp4")
        
        print("Uploading file...")
        await page.wait_for_timeout(15000)
        await page.screenshot(path="tiktok_upload_progress.png")
        print("Screenshot saved to tiktok_upload_progress.png")
        
        # Try to click post
        try:
            # We look for the "Pubblica" or "Post" button
            post_btn = page.locator("button:has-text('Pubblica'), button:has-text('Post')").last
            await post_btn.scroll_into_view_if_needed()
            await post_btn.click()
            print("Clicked post button!")
        except Exception as e:
            print(f"Could not click post: {e}")
            
        await page.wait_for_timeout(5000)
        await page.screenshot(path="tiktok_after_post.png")
        print("Screenshot saved to tiktok_after_post.png")

        await browser.close()

asyncio.run(upload_test())
