"""
Module C — Video Processing & Composition.

Pipeline:
1. Seleziona/scarica background video (locale o Pexels)
2. Crop/resize al ratio richiesto (16:9, 9:16, 1:1)
3. Loopa il video se più corto dell'audio
4. Applica caption animate sincronizzate ai segmenti dello script
5. Renderizza il video finale con audio embedded
"""

from __future__ import annotations
import math
import random
from pathlib import Path
from typing import TYPE_CHECKING

import PIL.Image
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS

import requests
from rich.console import Console

import config
from modules.script_manager import ParsedScript, split_into_caption_chunks

if TYPE_CHECKING:
    pass

console = Console()


# ── Background Video Selection ────────────────────────────────────────────────

def _list_local_backgrounds() -> list[Path]:
    exts = ("*.mp4", "*.mov", "*.avi", "*.mkv", "*.webm", "*.jpg", "*.jpeg", "*.png")
    vids: list[Path] = []
    for pat in exts:
        vids.extend(config.BG_DIR.glob(pat))
    return vids


def _download_pexels_video(keyword: str, dest_dir: Path) -> Path | None:
    """Scarica il primo video trovato su Pexels per la query data."""
    if not config.PEXELS_API_KEY:
        console.print("[yellow]⚠[/] PEXELS_API_KEY mancante, skip download Pexels")
        return None

    import random
    page = random.randint(1, 10)
    
    headers = {"Authorization": config.PEXELS_API_KEY}
    url = "https://api.pexels.com/videos/search"
    params = {
        "query": keyword,
        "per_page": config.PEXELS_PER_PAGE,
        "orientation": "portrait",
        "size": "large",
        "page": page
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        videos = resp.json().get("videos", [])
        import random
        random.shuffle(videos)
    except Exception as e:
        console.print(f"[red]Pexels API error:[/] {e}")
        return None

    for video in videos:
        duration = video.get("duration", 0)
        if duration < config.PEXELS_MIN_DURATION:
            continue
        # Cerca il file HD (720p o 1080p)
        files = sorted(
            video.get("video_files", []),
            key=lambda f: f.get("height", 0),
            reverse=True,
        )
        for vf in files:
            if vf.get("height", 0) >= 720:
                video_url = vf["link"]
                ext = video_url.split("?")[0].rsplit(".", 1)[-1]
                dest = dest_dir / f"pexels_{video['id']}.{ext}"
                if dest.exists():
                    return dest
                console.print(f"[cyan]Download[/] Pexels: {dest.name} ({duration}s)…")
                with requests.get(video_url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                console.print(f"[green]✓[/] scaricato: {dest}")
                return dest
    return None


def get_dynamic_background_videos(audio_duration: float, preferred_bg: str | Path | None = None, interval: float = 4.0) -> list[Path]:
    """
    Restituisce una lista di N background video adeguati per coprire la durata dell'audio,
    cambiando scena circa ogni `interval` secondi.
    """
    n_clips_needed = math.ceil(audio_duration / interval)
    local = _list_local_backgrounds()
    
    chosen_paths = []
    
    # 1. Se c'è un preferred_bg forzato, usiamo quello (magari per la prima scena o tutte)
    forced_bg = None
    if preferred_bg:
        pref_path = Path(preferred_bg)
        if pref_path.exists():
            forced_bg = pref_path
        else:
            matches = [f for f in local if str(preferred_bg).lower() in f.name.lower()]
            if matches:
                forced_bg = matches[0]

    console.print(f"[cyan]Cerco {n_clips_needed} clip video dinamiche...[/]")
    
    # 2. Raccogliamo N clip
    queries = list(config.PEXELS_SEARCH_QUERIES) if hasattr(config, "PEXELS_SEARCH_QUERIES") and config.PEXELS_SEARCH_QUERIES else ["nature", "abstract", "technology"]
    
    for i in range(n_clips_needed):
        if forced_bg and i == 0:
            chosen_paths.append(forced_bg)
            continue
            
        # Proviamo da pexels
        downloaded = None
        if getattr(config, "PEXELS_API_KEY", None):
            query = random.choice(queries)
            downloaded = _download_pexels_video(query, config.BG_DIR)
            
        if downloaded:
            chosen_paths.append(downloaded)
        elif local:
            chosen_paths.append(random.choice(local))
        elif forced_bg:
            chosen_paths.append(forced_bg)
        else:
            raise FileNotFoundError(
                "Nessun background video disponibile.\n"
                "Aggiungi un video in assets/backgrounds/ oppure configura PEXELS_API_KEY nel .env"
            )
            
    return chosen_paths


# ── Video Crop & Resize ───────────────────────────────────────────────────────

def _get_resolution(ratio_key: str) -> tuple[int, int]:
    w, h = config.SUPPORTED_RATIOS.get(ratio_key, config.SUPPORTED_RATIOS[config.DEFAULT_RATIO])
    return w, h


def crop_to_ratio(clip, target_w: int, target_h: int):
    """Centra e cropa il clip al ratio target senza distorcere."""
    from moviepy.editor import VideoFileClip
    from moviepy.video.fx.all import resize, crop

    src_w, src_h = clip.size
    target_ratio = target_w / target_h
    src_ratio    = src_w   / src_h

    if src_ratio > target_ratio:
        # più largo del target → crop laterale
        new_w = int(src_h * target_ratio)
        x1 = (src_w - new_w) // 2
        clip = crop(clip, x1=x1, x2=x1 + new_w)
    else:
        # più alto del target → crop verticale
        new_h = int(src_w / target_ratio)
        y1 = (src_h - new_h) // 2
        clip = crop(clip, y1=y1, y2=y1 + new_h)

    clip = resize(clip, (target_w, target_h))
    return clip


def _caption_font_path() -> str:
    p = Path(config.CAPTION_FONT)
    if p.exists():
        return str(p)
    return config.CAPTION_FALLBACK


def _render_pil_caption_np(text: str, video_w: int, font_size: int = 90):
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    font_path = _caption_font_path()
    try:
        font = ImageFont.truetype(font_path, font_size)
    except:
        try:
            font = ImageFont.truetype('/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf', font_size)
        except:
            font = ImageFont.load_default()

    words = text.split()
    lines = []
    curr = []
    for w in words:
        if len(" ".join(curr + [w])) <= 20:
            curr.append(w)
        else:
            lines.append(" ".join(curr))
            curr = [w]
    if curr:
        lines.append(" ".join(curr))

    line_str = "\n".join(lines)
    dummy_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = dummy_draw.multiline_textbbox((0, 0), line_str, font=font, align="center")
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    padding_x, padding_y = 50, 30
    box_w = int(min(video_w - 100, tw + padding_x * 2))
    box_h = int(th + padding_y * 2)

    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Sleek dark rounded pill rectangle with gold border
    draw.rounded_rectangle([0, 0, box_w, box_h], radius=18, fill=(15, 23, 42, 230), outline=(250, 204, 21, 255), width=3)

    # Text inside
    tx = int((box_w - tw) // 2)
    ty = int((box_h - th) // 2 - bbox[1])
    draw.multiline_text((tx, ty), line_str, font=font, fill=(255, 255, 255, 255), align="center")

    return np.array(img)


def _render_pil_title_np(title_text: str, video_w: int):
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    
    font_size = 75
    try:
        font = ImageFont.truetype('/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf', font_size)
    except:
        font = ImageFont.load_default()
        
    words = title_text.upper().split()
    lines = []
    curr = []
    for w in words:
        if len(" ".join(curr + [w])) <= 16:
            curr.append(w)
        else:
            lines.append(" ".join(curr))
            curr = [w]
    if curr:
        lines.append(" ".join(curr))
        
    line_str = "\n".join(lines)
    dummy_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = dummy_draw.multiline_textbbox((0, 0), line_str, font=font, align="center")
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    
    padding_x, padding_y = 60, 40
    box_w = int(min(video_w - 60, tw + padding_x * 2))
    box_h = int(th + padding_y * 2)
    
    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Sfondo scuro semitrasparente
    draw.rounded_rectangle([0, 0, box_w, box_h], radius=20, fill=(0, 0, 0, 180), outline=(255, 255, 255, 200), width=4)
    
    tx = int((box_w - tw) // 2)
    ty = int((box_h - th) // 2 - bbox[1])
    draw.multiline_text((tx, ty), line_str, font=font, fill=(255, 255, 255, 255), align="center", stroke_width=2, stroke_fill="black")
    
    return np.array(img)


def _render_pil_cta_card_np(video_w: int):
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    import os

    height = 550
    bg_path = config.ASSETS_DIR / "cta_bg.jpg"
    
    # Crea immagine vuota con alpha
    img = Image.new("RGBA", (video_w, height), (0, 0, 0, 0))
    margin = 45
    card_w = video_w - margin * 2
    card_h = height - 10
    
    if bg_path.exists():
        # Carica il super background premium
        try:
            bg_img = Image.open(str(bg_path)).convert("RGBA")
            # Ridimensiona l'immagine mantenendo le proporzioni e poi ritaglia al centro
            bg_ratio = bg_img.width / bg_img.height
            target_ratio = card_w / card_h
            if bg_ratio > target_ratio:
                # Troppo larga, fitta in altezza e taglia i lati
                new_h = card_h
                new_w = int(new_h * bg_ratio)
            else:
                new_w = card_w
                new_h = int(new_w / bg_ratio)
                
            bg_img = bg_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            # Ritaglia dal centro
            left = (new_w - card_w) // 2
            top = (new_h - card_h) // 2
            bg_img = bg_img.crop((left, top, left + card_w, top + card_h))
            
            # Arrotonda gli angoli applicando una maschera
            mask = Image.new("L", (card_w, card_h), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle([0, 0, card_w, card_h], radius=30, fill=255)
            
            # Incolla sulla canvas principale
            img.paste(bg_img, (margin, 10), mask=mask)
        except Exception as e:
            console.print(f"[red]Errore caricamento background CTA:[/] {e}")
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle([margin, 10, video_w - margin, height - 10], radius=30, fill=(15, 23, 42, 240))
    else:
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([margin, 10, video_w - margin, height - 10], radius=30, fill=(15, 23, 42, 240))

    # Aggiungiamo un leggero bordo e scuriamo appena per far leggere il testo
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([margin, 10, video_w - margin, height - 10], radius=30, fill=(0, 0, 0, 100), outline=(212, 175, 55, 255), width=4)

    try:
        font_title = ImageFont.truetype('/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf', 70)
        font_sub = ImageFont.truetype('/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf', 55)
    except:
        font_title = font_sub = ImageFont.load_default()

    # Title
    t1 = "📖 SCOPRI IL LIBRO SU AMAZON"
    bbox1 = draw.textbbox((0, 0), t1, font=font_title)
    w1 = bbox1[2] - bbox1[0]
    draw.text((int((video_w - w1)//2), 60), t1, font=font_title, fill=(255, 215, 0, 255))

    # Subtext button - elegant minimal button
    t2 = " CLICCA IL LINK IN BIO NEL PROFILO "
    bbox2 = draw.textbbox((0, 0), t2, font=font_sub)
    w2, h2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
    
    btn_y = 220
    draw.rounded_rectangle([int((video_w - w2)//2 - 40), btn_y, int((video_w + w2)//2 + 40), int(btn_y + h2 + 30)], radius=25, fill=(20, 20, 20, 230), outline=(255, 215, 0, 255), width=2)
    draw.text((int((video_w - w2)//2), btn_y + 10), t2, font=font_sub, fill=(255, 255, 255, 255))

    # Arrow indicator pointing down
    t3 = "⬇️ ACCEDI AL LINK ORA ⬇️"
    bbox3 = draw.textbbox((0, 0), t3, font=font_sub)
    w3 = bbox3[2] - bbox3[0]
    draw.text((int((video_w - w3)//2), 380), t3, font=font_sub, fill=(255, 215, 0, 255))

    return np.array(img)


def build_caption_clips(script: ParsedScript, video_w: int, video_h: int):
    """
    Crea una lista di ImageClip MoviePy usando PIL (nessuna dipendenza ImageMagick).
    """
    from moviepy.editor import ImageClip

    font_size = 34
    captions = []

    for seg in script.segments:
        chunks = split_into_caption_chunks(seg.text, config.CAPTION_MAX_CHARS)
        if not chunks:
            continue

        chunk_dur = seg.estimated_duration / len(chunks)
        for i, chunk in enumerate(chunks):
            t_start = seg.start_time + i * chunk_dur
            t_end   = t_start + chunk_dur
            fade    = min(config.CAPTION_FADE_DUR, chunk_dur * 0.2)

            try:
                img_np = _render_pil_caption_np(chunk, video_w, font_size=font_size)
                img_clip = (
                    ImageClip(img_np, transparent=True)
                    .set_start(t_start)
                    .set_end(t_end)
                    .set_position(("center", int(video_h * 0.70)))
                    .crossfadein(fade)
                    .crossfadeout(fade)
                )
                captions.append(img_clip)
            except Exception as e:
                console.print(f"[red]Caption PIL error:[/] {e}")

    # Aggiungi la CTA Card con le frecce per il finale del video
    if script.segments:
        last_seg = script.segments[-1]
        cta_start = max(0.0, script.total_estimated_duration - 10.0)
        cta_end = script.total_estimated_duration
        try:
            cta_np = _render_pil_cta_card_np(video_w)
            cta_clip = (
                ImageClip(cta_np, transparent=True)
                .set_start(cta_start)
                .set_end(cta_end)
                .set_position("center")
                .crossfadein(0.3)
            )
            captions.append(cta_clip)
            console.print(f"[bold gold1]✓ CTA Card & Freccia Link in Bio creata![/]")
        except Exception as e:
            console.print(f"[red]CTA Card error:[/] {e}")

    return captions


# ── Main Composer ─────────────────────────────────────────────────────────────

def compose_video(
    audio_path: Path,
    audio_duration: float,
    script: ParsedScript,
    output_path: Path,
    ratio_key: str = config.DEFAULT_RATIO,
    preferred_bg: str | Path | None = None,
) -> Path:
    """
    Assembla il video finale:
    - background loopato
    - audio voiceover
    - caption animate
    """
    from moviepy.editor import VideoFileClip, AudioFileClip, CompositeVideoClip, concatenate_videoclips

    target_w, target_h = _get_resolution(ratio_key)
    console.print(f"[bold]Composizione video[/] {target_w}×{target_h} @ {config.VIDEO_FPS}fps")

    # 1. Background Dinamico
    if preferred_bg and Path(preferred_bg).exists() and str(preferred_bg).lower().endswith(('.mp4', '.mov', '.webm', '.avi')):
        # Se è stato fornito un video di background esplicito, usalo per intero
        from moviepy.editor import VideoFileClip, concatenate_videoclips
        bg_clip = VideoFileClip(str(preferred_bg), audio=False)
        bg_clip = crop_to_ratio(bg_clip, target_w, target_h)
        if bg_clip.duration < audio_duration:
            n_loops = math.ceil(audio_duration / bg_clip.duration)
            bg_clip = concatenate_videoclips([bg_clip] * n_loops)
        bg = bg_clip.subclip(0, audio_duration).set_fps(config.VIDEO_FPS)
    else:
        interval = 3.5  # Cambia clip ogni 3.5 secondi
        bg_paths = get_dynamic_background_videos(audio_duration, preferred_bg=preferred_bg, interval=interval)
        
        bg_clips = []
        current_time = 0.0
        
        for i, p in enumerate(bg_paths):
            ext = str(p).lower().split('.')[-1]
            
            time_left = audio_duration - current_time
            target_clip_dur = min(interval, time_left)
            
            if ext in ['jpg', 'jpeg', 'png']:
                from moviepy.editor import ImageClip
                clip = ImageClip(str(p)).set_duration(target_clip_dur)
                clip = crop_to_ratio(clip, target_w, target_h)
                clip = clip.resize(lambda t: 1 + 0.02 * t)
                w, h = clip.size
                clip = clip.crop(x1=(w-target_w)//2, y1=(h-target_h)//2, x2=(w+target_w)//2, y2=(h+target_h)//2)
            else:
                from moviepy.editor import VideoFileClip, concatenate_videoclips
                clip = VideoFileClip(str(p), audio=False)
                clip = crop_to_ratio(clip, target_w, target_h)
                if clip.duration < target_clip_dur:
                    n_loops = math.ceil(target_clip_dur / clip.duration)
                    clip = concatenate_videoclips([clip] * n_loops)
                clip = clip.subclip(0, target_clip_dur)
                
            bg_clips.append(clip)
            current_time += target_clip_dur
            
            if current_time >= audio_duration:
                break

        bg = concatenate_videoclips(bg_clips).set_fps(config.VIDEO_FPS)
    bg = bg.without_audio()

    # 3. Caption (Hormozi style via Whisper)
    try:
        from modules.whisper_captions import generate_dynamic_captions
        caption_clips = generate_dynamic_captions(str(audio_path), target_w, target_h)
        
        # Aggiungiamo anche la CTA finale se ci sono script segments
        if script.segments:
            cta_start = max(0.0, audio_duration - 10.0)
            cta_end = audio_duration
            try:
                from moviepy.editor import ImageClip
                cta_np = _render_pil_cta_card_np(target_w)
                cta_clip = (
                    ImageClip(cta_np, transparent=True)
                    .set_start(cta_start)
                    .set_end(cta_end)
                    .set_position("center")
                    .crossfadein(0.3)
                )
                caption_clips.append(cta_clip)
            except Exception as e:
                console.print(f"[red]Errore CTA Card:[/] {e}")
                
    except Exception as e:
        console.print(f"[red]Errore Whisper ({e}), uso fallback standard...[/]")
        caption_clips = build_caption_clips(script, target_w, target_h)

    if caption_clips:
        console.print(f"[cyan]Caption:[/] {len(caption_clips)} clip generate")
        final = CompositeVideoClip([bg] + caption_clips, size=(target_w, target_h))
    else:
        console.print("[dim]Nessuna caption generata.[/]")
        final = bg

    # 4. Audio & SFX
    audio_clip = AudioFileClip(str(audio_path))
    audio_clips = [audio_clip]
    
    # SFX - Whoosh ad ogni cambio scena
    whoosh_file = config.ASSETS_DIR / "sfx" / "whoosh.mp3"
    if whoosh_file.exists():
        for t in range(1, math.ceil(audio_duration / interval)):
            time = t * interval
            if time < audio_duration - 1:
                try:
                    whoosh = AudioFileClip(str(whoosh_file)).set_start(time - 0.2).volumex(0.4)
                    audio_clips.append(whoosh)
                except Exception:
                    pass
                    
    # SFX - Pop quando compare la CTA Card
    pop_file = config.ASSETS_DIR / "sfx" / "pop.mp3"
    if pop_file.exists() and script.segments:
        cta_start = max(0.0, audio_duration - 10.0)
        try:
            pop = AudioFileClip(str(pop_file)).set_start(cta_start).volumex(0.7)
            audio_clips.append(pop)
        except Exception:
            pass
            
    from moviepy.editor import CompositeAudioClip
    final_audio = CompositeAudioClip(audio_clips)
    final = final.set_audio(final_audio).set_duration(audio_duration)

    # 5. Render
    output_path.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]Rendering…[/] → {output_path}")
    final.write_videofile(
        str(output_path),
        fps=config.VIDEO_FPS,
        codec=config.VIDEO_CODEC,
        bitrate=config.VIDEO_BITRATE,
        audio_codec=config.AUDIO_CODEC,
        temp_audiofile=str(config.TEMP_DIR / "temp_audio.m4a"),
        remove_temp=True,
        logger="bar",
    )

    # cleanup
    try:
        bg.close()
        audio_clip.close()
        final.close()
    except Exception:
        pass

    console.print(f"[bold green]Video finale:[/] {output_path}")
    return output_path
