import argparse
import sys
from pathlib import Path
from rich.console import Console
from modules.script_manager import load_script
from modules.audio_generator import generate_italian_voiceover
import config

console = Console()

def main():
    parser = argparse.ArgumentParser(description="Step 1: Generazione Voce")
    parser.add_argument("--script", required=True, help="Percorso script txt")
    parser.add_argument("--output", default="temp/voiceover.mp3", help="Output audio file")
    parser.add_argument("--provider", default="edge", choices=["edge", "elevenlabs", "xtts"])
    parser.add_argument("--voice", default=None, help="Speaker wav per xtts o nome per edge")
    
    args = parser.parse_args()
    
    script_path = Path(args.script)
    if not script_path.exists():
        console.print(f"[red]Script non trovato:[/] {script_path}")
        sys.exit(1)
        
    parsed = load_script(script_path)
    console.print(f"Generazione voce per: {script_path}")
    
    out_path = Path(args.output)
    generate_italian_voiceover(
        text=parsed.full_text,
        output_path=out_path,
        provider=args.provider,
        voice=args.voice,
        mix_ambient=True
    )
    console.print(f"[green]Step 1 Completato! Voce salvata in {out_path}[/]")

if __name__ == "__main__":
    main()
