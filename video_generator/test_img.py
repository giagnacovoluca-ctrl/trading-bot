import os
from google import genai
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

try:
    print("Testing image generation...")
    result = client.models.generate_images(
        model='imagen-3.0-generate-001',
        prompt='A beautiful abstract gradient background',
        number_of_images=1,
        aspect_ratio='9:16'
    )
    for generated_image in result.generated_images:
        print("Success! Image generated.")
except Exception as e:
    print(f"Error: {e}")
