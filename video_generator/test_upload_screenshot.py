import asyncio
from playwright.sync_api import sync_playwright
import time
from tiktok_uploader.upload import TikTokUploader

def test_upload():
    # create a dummy video
    with open("dummy.mp4", "wb") as f:
        f.write(b"\x00" * 1024 * 100) # 100KB dummy video
        
    print("Testing upload using TikTokUploader...")
    try:
        from tiktok_uploader.upload import upload_video
        
        failed = upload_video(
            "dummy.mp4",
            description="Test video",
            cookies="cookies.txt",
            headless=True
        )
        
        if failed:
            print("Upload failed:", failed)
        else:
            print("Upload succeeded!")
            
    except Exception as e:
        print(f"Error uploading: {e}")

if __name__ == "__main__":
    test_upload()
