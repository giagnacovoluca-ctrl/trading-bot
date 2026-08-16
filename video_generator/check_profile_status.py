import os
import sys
from playwright.sync_api import sync_playwright

PROFILE_DIR = "/home/ubuntu/GIT/video_generator/chrome_profile"

def check_status():
    print(f"Checking TikTok login status with profile: {PROFILE_DIR}")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
            ],
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        
        # Navigate to creator center / upload
        print("Navigating to creator center...")
        page.goto("https://www.tiktok.com/creator-center/upload?lang=en", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(10000) # Wait a bit for redirects/renders
        
        current_url = page.url
        print(f"Current URL: {current_url}")
        
        # Take screenshot
        screenshot_path = "profile_status_upload.png"
        page.screenshot(path=screenshot_path)
        print(f"Screenshot saved to {screenshot_path}")
        
        # Also check main page
        print("Navigating to main page...")
        page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
        print(f"Main page URL: {page.url}")
        page.screenshot(path="profile_status_main.png")
        print("Screenshot saved to profile_status_main.png")
        
        ctx.close()

if __name__ == "__main__":
    check_status()
