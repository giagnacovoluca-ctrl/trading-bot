import uuid
import logging
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import config
from modules.script_manager import parse_script, adjust_timing_to_audio
from modules.audio_generator import generate_italian_voiceover
from modules.video_composer import compose_video

app = FastAPI(
    title="Video Generator API", 
    description="API per n8n per generare video in automatico."
)

class VideoRequest(BaseModel):
    script_text: str
    voice: str = "elsa"  # elsa o diego
    ratio: str = "916"   # default per TikTok
    provider: str = "edge" # edge (gratis) o elevenlabs
    no_ambient: bool = False
    background: str | None = None # nome del file o None per scaricare da Pexels

class VideoResponse(BaseModel):
    success: bool
    video_path: str | None = None
    error: str | None = None

@app.post("/generate", response_model=VideoResponse)
def generate_video(req: VideoRequest):
    try:
        # 1. Parsing Script
        parsed = parse_script(req.script_text)
        
        # 2. Generazione Audio
        job_id = str(uuid.uuid4())[:8]
        audio_path = config.TEMP_DIR / f"voiceover_{job_id}.mp3"
        
        voice_map = {"elsa": config.EDGE_TTS_VOICE, "diego": config.EDGE_TTS_VOICE_M}
        resolved_voice = voice_map.get(req.voice, req.voice)
        
        audio_path, audio_duration = generate_italian_voiceover(
            text=parsed.full_text,
            output_path=audio_path,
            provider=req.provider,
            voice=resolved_voice,
            mix_ambient=not req.no_ambient,
        )
        
        # Sincronizza i tempi dello script con l'audio reale
        parsed = adjust_timing_to_audio(parsed, audio_duration)
        
        # 3. Composizione Video
        output_path = Path("output") / f"video_{job_id}.mp4"
        
        final_video = compose_video(
            audio_path=audio_path,
            audio_duration=audio_duration,
            script=parsed,
            output_path=output_path,
            ratio_key=req.ratio,
            preferred_bg=req.background,
        )
        
        # Ritorna il path assoluto così n8n sa dove prenderlo
        return VideoResponse(success=True, video_path=str(final_video.absolute()))

    except Exception as e:
        logging.error(f"Errore durante la generazione: {e}")
        return VideoResponse(success=False, error=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
