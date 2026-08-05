"""
Module D — Main Orchestrator (CLI via argparse).

Uso:
    python main.py generate --script scripts/input.txt --output output/video.mp4 --ratio 916
    python main.py generate --script scripts/input.txt --voice diego --no-ambient
    python main.py generate --script scripts/input.txt --provider elevenlabs
    python main.py test-tts "Ciao, questo è un test."
    python main.py list-voices
    python main.py generate --script scripts/input.txt --dry-run
"""

from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def _print_header():
    console.print(Panel.fit(
        "[bold cyan]Italian Video Generator[/]\n"
        "[dim]Voiceover neurali + background ipnotici + caption animate[/]",
        border_style="cyan",
    ))


def _summary_table(script_path: str, output_path: str, ratio: str, provider: str,
                   duration: float, n_segments: int):
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim")
    t.add_column(style="bold")
    t.add_row("Script",    script_path)
    t.add_row("Output",    output_path)
    t.add_row("Ratio",     ratio)
    t.add_row("Provider",  provider)
    t.add_row("Durata",    f"{duration:.1f}s")
    t.add_row("Segmenti",  str(n_segments))
    console.print(Panel(t, title="[bold green]✓ Video generato[/]", border_style="green"))


# ── Sub-commands ──────────────────────────────────────────────────────────────

def cmd_generate(args: argparse.Namespace) -> int:
    from modules.script_manager import load_script, adjust_timing_to_audio
    from modules.audio_generator import generate_italian_voiceover
    from modules.video_composer  import compose_video
    import config

    script = Path(args.script)
    if not script.exists():
        console.print(f"[red]Script non trovato:[/] {script}")
        return 1

    if args.ratio not in config.SUPPORTED_RATIOS:
        console.print(f"[red]Ratio non valido:[/] {args.ratio}. Validi: {list(config.SUPPORTED_RATIOS)}")
        return 1

    voice_map = {"elsa": config.EDGE_TTS_VOICE, "diego": config.EDGE_TTS_VOICE_M}
    resolved_voice = voice_map.get(args.voice, args.voice) if args.voice else None

    # 1. Parsing script
    console.print(f"\n[bold]1/4 — Parsing script:[/] {script}")
    parsed = load_script(script)
    console.print(
        f"[dim]→ {parsed.word_count} parole | {len(parsed.segments)} segmenti | "
        f"durata stimata: {parsed.total_estimated_duration:.1f}s[/]"
    )

    if args.dry_run:
        console.print("\n[yellow]DRY RUN — nessun file generato.[/]")
        for i, seg in enumerate(parsed.segments):
            preview = seg.text[:60].replace("\n", " ")
            console.print(f"  [{i}] {seg.start_time:.1f}s → {seg.end_time:.1f}s | {preview}…")
        return 0

    # 2. Audio
    console.print(f"\n[bold]2/4 — Generazione voiceover[/] ({args.provider})")
    audio_path = config.TEMP_DIR / "voiceover.mp3"
    audio_path, audio_duration = generate_italian_voiceover(
        text=parsed.full_text,
        output_path=audio_path,
        provider=args.provider,
        voice=resolved_voice,
        mix_ambient=not args.no_ambient,
    )
    parsed = adjust_timing_to_audio(parsed, audio_duration)
    console.print(f"[dim]→ durata audio reale: {audio_duration:.1f}s[/]")

    # 3. Video
    output = Path(args.output)
    console.print(f"\n[bold]3/4 — Composizione video[/] (ratio={args.ratio})")
    final_video = compose_video(
        audio_path=audio_path,
        audio_duration=audio_duration,
        script=parsed,
        output_path=output,
        ratio_key=args.ratio,
        preferred_bg=args.background or script.stem,
    )

    # 4. Summary
    console.print(f"\n[bold]4/4 — Completato[/]")
    _summary_table(str(script), str(final_video), args.ratio, args.provider,
                   audio_duration, len(parsed.segments))
    return 0


def cmd_test_tts(args: argparse.Namespace) -> int:
    from modules.audio_generator import generate_italian_voiceover
    import config

    out = config.TEMP_DIR / "test_tts.mp3"
    path, dur = generate_italian_voiceover(
        args.text, out, provider=args.provider, mix_ambient=False
    )
    console.print(f"[bold green]Test OK[/] → {path} ({dur:.1f}s)")
    return 0


def cmd_list_voices(_args: argparse.Namespace) -> int:
    import edge_tts

    async def _list():
        voices = await edge_tts.list_voices()
        it_voices = [v for v in voices if v["Locale"].startswith("it-IT")]
        t = Table("ShortName", "Gender", "FriendlyName")
        for v in it_voices:
            t.add_row(v["ShortName"], v["Gender"], v["FriendlyName"])
        console.print(t)

    asyncio.run(_list())
    return 0


# ── Parser ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video_generator",
        description="Genera video italiani con voiceover neurale + caption animate",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # generate
    gen = sub.add_parser("generate", help="Genera un video completo")
    gen.add_argument("--script",   "-s", required=True, help="Percorso script .txt o .md")
    gen.add_argument("--output",   "-o", default="output/video.mp4", help="File output .mp4")
    gen.add_argument("--ratio",    "-r", default="916",
                     help="Ratio: 916 (TikTok/Reels, default) | 169 (YouTube) | 11 (Square)")
    gen.add_argument("--provider", "-p", default="edge",
                     choices=["edge", "elevenlabs", "xtts"],
                     help="TTS: edge (gratuito) | elevenlabs (premium) | xtts (clonazione vocale locale)")
    gen.add_argument("--voice",    "-v", default=None,
                     help="Voce edge-tts: elsa (F, default) | diego (M)")
    gen.add_argument("--background", "--bg", default=None,
                     help="Nome o percorso del background video da usare (es. meditazione_zen_ad_hoc.mp4)")
    gen.add_argument("--no-ambient", action="store_true",
                     help="Disabilita traccia ambient sotto il voiceover")
    gen.add_argument("--dry-run", action="store_true",
                     help="Mostra parsing senza generare file")

    # test-tts
    tts = sub.add_parser("test-tts", help="Testa la voce TTS con un testo breve")
    tts.add_argument("text", nargs="?",
                     default="Benvenuto nel futuro dell'automazione video.",
                     help="Testo da sintetizzare")
    tts.add_argument("--provider", "-p", default="edge", choices=["edge", "elevenlabs"])

    # list-voices
    sub.add_parser("list-voices", help="Elenca le voci italiane edge-tts disponibili")

    return parser


def main():
    _print_header()
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "generate":    cmd_generate,
        "test-tts":    cmd_test_tts,
        "list-voices": cmd_list_voices,
    }
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(fn(args))


if __name__ == "__main__":
    main()
