import os
import sys
import time
import random
import argparse
import subprocess
from pathlib import Path
from rich.console import Console

console = Console()

# Argomenti random se non ne viene fornito uno
TOPIC_IDEAS = [
    "energia e digestione",
    "meditazione e stress",
    "fisica quantistica e mente",
    "scienza e vibrazioni",
    "alimentazione e umore",
    "frequenze del pensiero",
    "cibo per il cervello"
]

def run_step(command: list[str], step_name: str):
    """Esegue un processo esterno e controlla eventuali errori (OOM, eccezioni)."""
    console.print(f"\n[bold magenta]▶ AVVIO {step_name}[/]")
    console.print(f"[dim]Comando: {' '.join(command)}[/dim]")
    
    start_time = time.time()
    
    try:
        # Usa subprocess.run per aspettare la fine dell'esecuzione
        # L'output viene stampato direttamente in console
        result = subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        console.print(f"\n[bold red]✖ ERRORE FATALE IN {step_name}[/]")
        console.print(f"Codice di uscita: {e.returncode}")
        sys.exit(1)
        
    durata = time.time() - start_time
    console.print(f"[bold green]✓ {step_name} COMPLETATO con successo in {durata:.1f} secondi.[/]\n")

def main():
    parser = argparse.ArgumentParser(description="Agente Orchestratore TikTok")
    parser.add_argument("--topic", help="Tema del video (opzionale, altrimenti ne pesca uno a caso dai tuoi libri)")
    parser.add_argument("--mode", default="promo", choices=["promo", "virale", "bastian"], help="Modalità video")
    args = parser.parse_args()

    topic = args.topic or random.choice(TOPIC_IDEAS)
    
    console.print(f"[bold blue]🤖 AGENTE TIKTOK INIZIALIZZATO[/]")
    console.print(f"Tema scelto per oggi: [bold yellow]'{topic}'[/]")
    console.print("=" * 60)
    
    # Python executable corrente (del virtual environment)
    python_exe = sys.executable

    # File paths
    script_txt = "scripts/script_generato.txt"
    audio_mp3 = "temp/voice.mp3"
    video_base = "temp/video_base.mp4"
    video_finale = "output/video_finale.mp4"
    
    # Assicurati che non ci siano vecchi file in giro
    for f in [script_txt, audio_mp3, video_base]:
        if Path(f).exists():
            Path(f).unlink()

    bg_dir = Path("assets/backgrounds")
    bg_dir.mkdir(parents=True, exist_ok=True)
    old_bg_files = set(bg_dir.glob("*.*"))

    # STEP 0: Generazione Testo (RAG)
    step0_cmd = [
        python_exe, "rag_generator.py", 
        "--topic", topic, 
        "--output", script_txt,
        "--mode", args.mode
    ]
    run_step(step0_cmd, "STEP 0: Ricerca e Scrittura Copione (Gemini API)")

    # Recupero forzato delle immagini generate dall'agente RAG (che le salva nella sua cartella artefatti)
    console.print("[dim]Recupero eventuali immagini generate dall'agente...[/]")
    try:
        subprocess.run("find ~/.gemini/antigravity-cli/brain/ -type f -mmin -5 \( -name '*.jpg' -o -name '*.png' \) -exec cp {} assets/backgrounds/ \;", shell=True)
    except Exception:
        pass

    new_bg_files = list(set(bg_dir.glob("*.*")) - old_bg_files)

    # Assicurati che lo script esista e non sia vuoto
    script_path = Path(script_txt)
    if not script_path.exists() or script_path.stat().st_size == 0:
        console.print("[red]Errore: Lo script non è stato generato o è vuoto.[/]")
        sys.exit(1)
        
    # Estrai il titolo e pulisci lo script per la voce
    script_content = script_path.read_text(encoding="utf-8").splitlines()
    hook_title = "SCOPERTA SHOCK"
    clean_lines = []
    for line in script_content:
        if line.startswith("TITOLO:"):
            hook_title = line.replace("TITOLO:", "").strip()
        else:
            clean_lines.append(line)
            
    # Sovrascrivi il file pulito senza il titolo (così la voce non lo legge)
    script_path.write_text("\n".join(clean_lines).strip(), encoding="utf-8")
    
    # Salva il titolo nello storico per non ripetere la notizia
    with open("used_news_history.txt", "a", encoding="utf-8") as f:
        f.write(hook_title + "\n")
    
    # Crea il filename basato sul titolo
    safe_title = "".join([c if c.isalnum() else "_" for c in hook_title.lower()]).strip("_")
    # rimuovi underscore multipli
    import re
    safe_title = re.sub(r"_+", "_", safe_title)
    if not safe_title: safe_title = "video"
    video_finale = f"output/video_{safe_title}.mp4"

    # Usa XTTS v2 (Modello Locale Open Source) per una voce super performante gratuita
    provider = "xtts"

    # STEP 1: Generazione Voce
    step1_cmd = [
        python_exe, "step1_voce.py", 
        "--script", script_txt, 
        "--output", audio_mp3, 
        "--provider", provider,
        "--voice", "assets/voices/mia_voce.wav"  # Il file audio da clonare
    ]
    run_step(step1_cmd, f"STEP 1: Generazione Voce Clonate Locale ({provider.upper()})")

    # STEP 2: Generazione Sfondo (Pexels / Immagini Generate)
    step2_cmd = [
        python_exe, "step2_sfondo.py", 
        "--audio", audio_mp3, 
        "--output", video_base,
        "--interval", "3.5",
        "--topic", topic
    ]
    if new_bg_files:
        step2_cmd.append("--images")
        step2_cmd.extend([str(f) for f in new_bg_files])
        console.print(f"Passo {len(new_bg_files)} immagini appena generate allo step 2.")
        
    run_step(step2_cmd, "STEP 2: Download Sfondo e Montaggio")

    # STEP 3: Sottotitoli Dinamici (Whisper)
    step3_cmd = [
        python_exe, "step3_sottotitoli.py", 
        "--video", video_base, 
        "--audio", audio_mp3, 
        "--output", video_finale,
        "--hook_title", hook_title
    ]
    if args.mode == "promo":
        step3_cmd.append("--cta")
        
    run_step(step3_cmd, "STEP 3: Sottotitoli Dinamici e CTA (Whisper)")

    # STEP 4: Pubblicazione Automatica (Opzionale)
    cookies_path = Path("cookies.txt")
    if cookies_path.exists():
        console.print("\n[bold magenta]🚀 Cookie trovati: Avvio pubblicazione automatica su TikTok...[/]")
        step4_cmd = [
            python_exe, "step4_pubblica.py", 
            "--video", video_finale, 
            "--script", script_txt,
            "--cookies", str(cookies_path),
            "--mode", args.mode
        ]
        run_step(step4_cmd, "STEP 4: Generazione Metadata e Upload TikTok")
    else:
        console.print("\n[dim]Salto la pubblicazione automatica: file 'cookies.txt' non trovato nella cartella principale.[/]")

    console.print("\n" + "="*60)
    console.print(f"🎉 [bold magenta]PROCESSO COMPLETATO![/]")
    console.print(f"Il tuo video finale è pronto in: [bold green]{video_finale}[/]")
    console.print("="*60 + "\n")

if __name__ == "__main__":
    main()
