from playwright.sync_api import sync_playwright
import time
import subprocess
import os

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1080, 'height': 1920}) # Mobile view 9:16
    page.goto("https://conscia-mente.vercel.app/links")
    time.sleep(3) # wait for page to load
    page.screenshot(path="temp/links_screenshot.png")
    browser.close()

ffmpeg_cmd = [
    "ffmpeg", "-y", "-loop", "1", "-i", "temp/links_screenshot.png", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
    "-c:v", "libx264", "-t", "4", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-shortest",
    "-vf", "scale=1080:1920,drawtext=text='⬇ CLICCA QUI ⬇':fontcolor=red:fontsize=100:x=(w-text_w)/2:y=(h/2)-200:box=1:boxcolor=yellow@0.8:boxborderw=10",
    "temp/cta_video.mp4"
]
subprocess.run(ffmpeg_cmd)
