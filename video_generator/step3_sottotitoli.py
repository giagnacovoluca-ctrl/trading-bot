import argparse
import sys
from pathlib import Path
from rich.console import Console
from moviepy.editor import VideoFileClip, CompositeVideoClip, ImageClip
from modules.whisper_captions import generate_dynamic_captions
from modules.video_composer import _render_pil_cta_card_np
import config

console = Console()

def main():
    parser = argparse.ArgumentParser(description="Step 3: Sottotitoli Dinamici")
    parser.add_argument("--video", required=True, help="Video base dello step 2")
    parser.add_argument("--audio", required=True, help="Audio per analizzare i sottotitoli (o video stesso)")
    parser.add_argument("--output", default="output/video_finale.mp4", help="Video finale TikTok")
    parser.add_argument("--cta", action="store_true", help="Aggiungi la CTA finale")
    parser.add_argument("--cta-title", default="RISORSA GRATUITA", help="Titolo CTA coerente con la risorsa")
    parser.add_argument("--cta-detail", default="SCOPRI IL CONTENUTO COLLEGATO", help="Dettaglio CTA")
    parser.add_argument("--cta-action", default="LINK IN BIO", help="Azione CTA")
    parser.add_argument("--hook_title", default="", help="Titolo gancio da mostrare in alto all'inizio del video")

    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        console.print(f"[red]Video non trovato:[/] {video_path}")
        sys.exit(1)

    base_video = VideoFileClip(str(video_path))
    target_w, target_h = base_video.size

    console.print(f"Estrazione sottotitoli tramite Whisper...")
    captions = generate_dynamic_captions(args.audio, target_w, target_h)

    # Crea il titolo in alto (se presente)
    if args.hook_title:
        try:
            from modules.video_composer import _render_pil_title_np
            title_np = _render_pil_title_np(args.hook_title, target_w)
            title_clip = (
                ImageClip(title_np, transparent=True)
                .set_start(0)
                .set_end(min(config.HOOK_DURATION, base_video.duration))
                .set_position(("center", 180))
                .crossfadeout(0.5)
            )
            captions.insert(0, title_clip)
        except Exception as e:
            console.print(f"[red]Errore generazione titolo Hook:[/] {e}")

    if args.cta:
        cta_start = max(0.0, base_video.duration - 7.0)
        cta_np = _render_pil_cta_card_np(
            target_w,
            title=args.cta_title,
            detail=args.cta_detail,
            action=args.cta_action,
        )
        cta_clip = (
            ImageClip(cta_np, transparent=True)
            .set_start(cta_start)
            .set_end(base_video.duration)
            .set_position("center")
            .crossfadein(0.3)
        )
        captions.append(cta_clip)

    console.print(f"Applicazione {len(captions)} clip di testo sul video...")

    # I sottotitoli e il titolo (già inserito in cima) verranno renderizzati senza filtri errati
    final = CompositeVideoClip([base_video] + captions, size=(target_w, target_h))
    final = final.set_duration(base_video.duration)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    final.write_videofile(
        str(out_path),
        fps=config.VIDEO_FPS,
        codec=config.VIDEO_CODEC,
        bitrate=config.VIDEO_BITRATE,
        audio_codec=config.AUDIO_CODEC,
        preset=config.VIDEO_PRESET,
        threads=config.VIDEO_THREADS,
        logger="bar"
    )
    console.print(f"[green]Step 3 Completato! Video FINALE pronto in {out_path}[/]")

if __name__ == "__main__":
    main()
