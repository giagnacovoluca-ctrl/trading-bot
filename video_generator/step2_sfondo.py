import os
import argparse
import sys
import math
import subprocess
from pathlib import Path
from rich.console import Console
from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips, ImageClip
from modules.video_composer import get_dynamic_background_videos, crop_to_ratio, _get_resolution
import config

console = Console()

def main():
    parser = argparse.ArgumentParser(description="Step 2: Video Base (Senza Sottotitoli)")
    parser.add_argument("--audio", required=True, help="Audio generato dallo step 1")
    parser.add_argument("--output", default="temp/video_base.mp4", help="Video output temporaneo")
    parser.add_argument("--ratio", default="916")
    parser.add_argument("--interval", type=float, default=3.5, help="Cambio scena ogni X sec")
    parser.add_argument("--images", nargs="+", help="Lista di immagini specifiche da usare come sfondo")
    parser.add_argument("--topic", help="Argomento per scaricare video coerenti da Pexels")
    
    args = parser.parse_args()
    
    audio_path = Path(args.audio)
    if not audio_path.exists():
        console.print(f"[red]Audio non trovato:[/] {audio_path}")
        sys.exit(1)
        
    audio_clip = AudioFileClip(str(audio_path))
    audio_dur = audio_clip.duration
    audio_clip.close()
    
    target_w, target_h = _get_resolution(args.ratio)
    
    n_clips_needed = math.ceil(audio_dur / args.interval)
    gen_images = [Path(p) for p in args.images] if args.images else []
    
    if gen_images or args.topic:
        console.print(f"Preparo {n_clips_needed} clip mixando immagini generate e video Pexels...")
        
        pexels_vids = []
        if args.topic and getattr(config, "PEXELS_API_KEY", None):
            stop_words = {"e", "a", "il", "la", "le", "lo", "gli", "i", "un", "uno", "una", "di", "da", "in", "con", "su", "per", "tra", "fra", "che", "del", "della", "dei", "delle", "al", "allo", "alla", "ai", "agli", "alle", "nel", "nella", "nei", "nelle", "sul", "sulla", "sui", "sulle", "come", "non", "più"}
            topic_words = [w for w in args.topic.split() if w.lower() not in stop_words and len(w) > 2]
            if not topic_words:
                topic_words = [args.topic] # fallback
            from modules.video_composer import _download_pexels_video
            # Scarica più video per evitare ripetizioni variando la keyword
            for i in range(min(10, n_clips_needed)):
                query = topic_words[i % len(topic_words)]
                vid = _download_pexels_video(query, config.BG_DIR)
                if vid and vid not in pexels_vids:
                    pexels_vids.append(vid)
                    
        pool = []
        img_idx = 0
        vid_idx = 0
        
        for i in range(n_clips_needed):
            use_img = False
            if gen_images and pexels_vids:
                use_img = (i % 2 == 0)
            elif gen_images:
                use_img = True
            elif pexels_vids:
                use_img = False
            else:
                use_img = True
                
            if use_img and gen_images:
                pool.append(gen_images[img_idx % len(gen_images)])
                img_idx += 1
            elif pexels_vids:
                pool.append(pexels_vids[vid_idx % len(pexels_vids)])
                vid_idx += 1
                
        if pool:
            bg_paths = pool
        else:
            bg_paths = get_dynamic_background_videos(audio_dur, interval=args.interval)
    else:
        console.print(f"Cerco sfondi per {audio_dur:.1f} secondi...")
        bg_paths = get_dynamic_background_videos(audio_dur, interval=args.interval)
    
    temp_dir = Path("temp/segments")
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    segment_files = []
    current_time = 0.0
    
    console.print("[cyan]Elaborazione singole clip (per evitare OOM)...[/]")
    for i, p in enumerate(bg_paths):
        ext = str(p).lower().split('.')[-1]
        time_left = audio_dur - current_time
        target_clip_dur = min(args.interval, time_left)

        if ext in ['jpg', 'jpeg', 'png']:
            clip = ImageClip(str(p)).set_duration(target_clip_dur)
            clip = crop_to_ratio(clip, target_w, target_h)
            # Applica un minuscolo resize per dare un effetto di movimento (Ken Burns)
            clip = clip.resize(lambda t: 1 + 0.02 * t)
            # Crop al centro dopo il resize
            w, h = clip.size
            clip = clip.crop(x1=(w-target_w)//2, y1=(h-target_h)//2, x2=(w+target_w)//2, y2=(h+target_h)//2)
        else:
            clip = VideoFileClip(str(p), audio=False)
            clip = crop_to_ratio(clip, target_w, target_h)
            if clip.duration < target_clip_dur:
                n_loops = math.ceil(target_clip_dur / clip.duration)
                clip = concatenate_videoclips([clip] * n_loops)
            clip = clip.subclip(0, target_clip_dur)
            
        clip = clip.set_fps(config.VIDEO_FPS)
        
        seg_out = temp_dir / f"seg_{i:03d}.mp4"
        clip.write_videofile(
            str(seg_out), 
            fps=config.VIDEO_FPS, 
            codec=config.VIDEO_CODEC, 
            bitrate=config.VIDEO_BITRATE,
            audio=False,
            logger=None
        )
        clip.close()
        segment_files.append(seg_out)
        
        current_time += target_clip_dur
        if current_time >= audio_dur:
            break

    # Usa FFMPEG per concatenare le clip elaborate e unire l'audio
    # Questo usa letteralmente zero RAM.
    concat_list = temp_dir / "concat_list.txt"
    with open(concat_list, "w") as f:
        for seg in segment_files:
            f.write(f"file '{seg.absolute()}'\n")
            
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    console.print("[cyan]Unione clip e audio con FFmpeg...[/]")
    ffmpeg_cmd = [
        config._FFMPEG_BIN, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-i", str(audio_path),
        "-c:v", "copy",
        "-c:a", "aac",
        "-map", "0:v",
        "-map", "1:a",
        "-shortest",
        str(out_path)
    ]
    
    subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    
    # Pulizia temporanei
    for seg in segment_files:
        seg.unlink()
    concat_list.unlink()
    
    console.print(f"[green]Step 2 Completato! Video base salvato in {out_path}[/]")

if __name__ == "__main__":
    main()
