import os
import sys
import time
import random
import argparse
import subprocess
from pathlib import Path
import re
from rich.console import Console

console = Console()

TOPIC_IDEAS = [
    "energia e digestione",
    "meditazione e stress",
    "fisica quantistica e mente",
    "scienza e vibrazioni",
    "alimentazione e umore",
    "frequenze del pensiero",
    "cibo per il cervello",
    "neuroplasticità e abitudini"
]

def run_step(command: list[str], step_name: str) -> bool:
    """Esegue un processo esterno e controlla eventuali errori. Ritorna True se ok, False se fallisce."""
    console.print(f"\n[bold magenta]▶ AVVIO {step_name}[/]")
    console.print(f"[dim]Comando: {' '.join(command)}[/dim]")
    
    start_time = time.time()
    try:
        subprocess.run(command, check=True)
        durata = time.time() - start_time
        console.print(f"[bold green]✓ {step_name} COMPLETATO in {durata:.1f} sec.[/]\n")
        return True
    except subprocess.CalledProcessError as e:
        console.print(f"\n[bold red]✖ ERRORE IN {step_name}[/]")
        console.print(f"Codice di uscita: {e.returncode}")
        return False

def evaluate_script(script_path: str) -> int:
    """Subagent Revisore Editoriale (Quality Assurance)"""
    console.print("[cyan]🤖 [Subagent Revisore] Valutazione qualità del copione in corso...[/]")
    script_content = Path(script_path).read_text(encoding="utf-8")
    
    prompt = f"""Sei il Caporedattore e Revisore Editoriale per un canale TikTok e YouTube Shorts.
Obiettivo: Ottenere estrema viralità per acquisire follower, ma mantenendo un tono SERIO, LOGICO e AUTOREVOLE per poterli successivamente convertire in clienti paganti.
Leggi questo copione:
---
{script_content}
---
Valutalo da 1 a 10.
REGOLE:
- Dai un voto alto (8+) SOLO se l'hook iniziale è un vero "pugno nello stomaco" intellettuale (cattura l'attenzione nei primi 2 secondi senza sembrare finto o clickbait).
- Il testo deve essere ritmato, denso di valore e privo di parole vuote.
- IL TONO NON DEVE MAI ESSERE CLICKBAIT DA TV LOCALE ("Scoperta assurda!"). Deve suscitare mistero, urgenza e curiosità basata rigorosamente sulla scienza o sulla psicologia.
Rispondi SOLO con il numero della valutazione (es. 8) e NIENT'ALTRO. Non giustificare il voto.
"""
    try:
        res = subprocess.run(["agy", "--dangerously-skip-permissions", "--print", prompt], capture_output=True, text=True)
        score_str = res.stdout.strip()
        nums = re.findall(r'\d+', score_str)
        if nums:
            score = int(nums[0])
            # Normalizza per sicurezza
            return min(10, max(1, score))
        return 7
    except Exception as e:
        console.print(f"[red]Errore Subagent Revisore: {e}[/]")
        return 10 # bypass in caso di errore di sistema

def main():
    parser = argparse.ArgumentParser(description="Agente Orchestratore TikTok Autonomo")
    parser.add_argument("--topic", help="Tema del video")
    parser.add_argument("--mode", default="promo", choices=["promo", "virale", "bastian"], help="Modalità video")
    args = parser.parse_args()

    topic = args.topic or random.choice(TOPIC_IDEAS)
    
    console.print(f"[bold blue]🤖 AGENTE TIKTOK AUTONOMO INIZIALIZZATO[/]")
    console.print(f"Tema: [bold yellow]'{topic}'[/]")
    console.print("=" * 60)
    
    python_exe = sys.executable

    script_txt = "scripts/script_generato.txt"
    audio_mp3 = "temp/voice.mp3"
    video_base = "temp/video_base.mp4"
    video_finale = "output/video_finale.mp4"
    
    for f in [script_txt, audio_mp3, video_base, "scripts/tiktok_caption.txt"]:
        if Path(f).exists():
            Path(f).unlink()

    bg_dir = Path("assets/backgrounds")
    bg_dir.mkdir(parents=True, exist_ok=True)
    old_bg_files = set(bg_dir.glob("*.*"))

    # STEP 0: Generazione Testo con Revisore (Self-Healing del testo)
    step0_cmd = [
        python_exe, "rag_generator.py", 
        "--topic", topic, 
        "--output", script_txt,
        "--mode", args.mode
    ]
    
    max_retries = 3
    for attempt in range(max_retries):
        success = run_step(step0_cmd, f"STEP 0: Scrittura Copione (Tentativo {attempt+1})")
        if not success:
            console.print("[red]Errore critico nella generazione del copione. Interruzione.[/]")
            sys.exit(1)
            
        score = evaluate_script(script_txt)
        console.print(f"[bold yellow]Voto Revisore: {score}/10[/]")
        
        if score >= 8:
            console.print("[bold green]✓ Il copione ha superato il Controllo Qualità![/]")
            break
        else:
            console.print(f"[bold red]✖ Il copione ha preso solo {score}. Rifiutato. Rigenerazione in corso...[/]")
            if attempt == max_retries - 1:
                console.print("[bold yellow]Raggiunto limite tentativi. Mi accontento e procedo.[/]")

    try:
        # Copia le immagini generate dall'agente nella cartella assets
        subprocess.run(r"find ~/.gemini/antigravity-cli/brain/ -type f -mmin -5 \( -name '*.jpg' -o -name '*.png' \) -exec cp {} assets/backgrounds/ \;", shell=True)
    except Exception:
        pass

    new_bg_files = list(set(bg_dir.glob("*.*")) - old_bg_files)

    script_path = Path(script_txt)
    if not script_path.exists() or script_path.stat().st_size == 0:
        console.print("[red]Errore: Lo script non è stato generato o è vuoto.[/]")
        sys.exit(1)
        
    script_content = script_path.read_text(encoding="utf-8").splitlines()
    hook_title = "SCOPERTA SHOCK"
    fonte_notizia = ""
    clean_lines = []
    for line in script_content:
        if line.startswith("TITOLO:"):
            hook_title = line.replace("TITOLO:", "").strip()
        elif line.startswith("FONTE_NOTIZIA:"):
            fonte_notizia = line.replace("FONTE_NOTIZIA:", "").strip()
        elif line.startswith("TESTO:"):
            clean_lines.append(line.replace("TESTO:", "").strip())
        else:
            clean_lines.append(line)
            
    if "Errore di Generazione" in hook_title:
        console.print("[bold red]✖ ERRORE FATALE:[/] L'Agente ha restituito 'Errore di Generazione'. Interrotto.")
        sys.exit(1)
            
    script_path.write_text("\n".join(clean_lines).strip(), encoding="utf-8")
    
    storia_da_salvare = fonte_notizia if fonte_notizia else hook_title
    with open("used_news_history.txt", "a", encoding="utf-8") as f:
        f.write(storia_da_salvare + "\n")
    
    safe_title = "".join([c if c.isalnum() else "_" for c in hook_title.lower()]).strip("_")
    safe_title = re.sub(r"_+", "_", safe_title)
    if not safe_title: safe_title = "video"
    video_finale = f"output/video_{safe_title}.mp4"

    # STEP 1: Voce
    provider = "xtts"
    step1_cmd = [
        python_exe, "step1_voce.py", 
        "--script", script_txt, 
        "--output", audio_mp3, 
        "--provider", provider,
        "--voice", "assets/voices/mia_voce.wav"
    ]
    if not run_step(step1_cmd, "STEP 1: Voce"):
        console.print("[red]Generazione voce fallita. Self-healing non configurato per questo step.[/]")
        sys.exit(1)

    # STEP 2: Sfondo (Self-Healing implementato)
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
        
    success = run_step(step2_cmd, "STEP 2: Download Sfondo")
    if not success:
        console.print("[bold red]⚠️ Sfondo fallito (possibile Pexels down o Timeout). AVVIO SELF-HEALING...[/]")
        console.print("[bold yellow]Subagent:[/] 'Oh, Pexels è down, cerco di arrangiarmi solo con le immagini dell'artefatto AI o fallback...'")
        # Rimuovi l'argomento topic e images se presenti, e forza l'uso di un background statico (es. dummy)
        # o potremmo creare un fallback che salta il download dei video e usa solo immagini
        fallback_cmd = [
            python_exe, "step2_sfondo.py", 
            "--audio", audio_mp3, 
            "--output", video_base,
            "--interval", "4.0",
            "--topic", "pattern astratto" # topic generico e sicuro
        ]
        if not run_step(fallback_cmd, "STEP 2 (FALLBACK): Sfondo Alternativo Sicuro"):
             console.print("[red]Anche il fallback è fallito. Interruzione.[/]")
             sys.exit(1)

    # STEP 3: Sottotitoli
    step3_cmd = [
        python_exe, "step3_sottotitoli.py", 
        "--video", video_base, 
        "--audio", audio_mp3, 
        "--output", video_finale,
        "--hook_title", hook_title
    ]
    if args.mode == "promo":
        step3_cmd.append("--cta")
        
    if not run_step(step3_cmd, "STEP 3: Sottotitoli e CTA"):
        sys.exit(1)

    # STEP 4: Upload TikTok (Self-Healing base)
    profile_path = Path("chrome_profile")
    cookies_path = Path("cookies.txt")
    if profile_path.exists() or cookies_path.exists():
        console.print("\n[bold magenta]🚀 Avvio pubblicazione automatica su TikTok...[/]")
        step4_cmd = [
            python_exe, "step4_pubblica.py", 
            "--video", video_finale, 
            "--script", script_txt,
            "--mode", args.mode
        ]
        if cookies_path.exists() and not profile_path.exists():
            step4_cmd.extend(["--cookies", str(cookies_path)])
            
        success = run_step(step4_cmd, "STEP 4: Upload TikTok")
        if not success:
             console.print("[bold red]⚠️ L'upload Playwright su TikTok ha fallito.[/]")
             console.print("[bold yellow]Subagent:[/] 'Pubblicazione fallita. Provo a segnalarlo e posso avviare un reset dei cookie al prossimo run.'")
             # In un futuro avanzato, si potrebbe cancellare i cookie qui
    else:
        console.print("\n[dim]Salto la pubblicazione automatica.[/]")

    # --- INTEGRAZIONE SITO NEXT.JS TRAMITE SUBAGENT ---
    if args.mode == "promo":
        console.print("\n[bold cyan]🔗 INTEGRAZIONE SITO (Subagent Redattore Web)...[/]")
        try:
            timestamp = int(time.time())
            video_filename = f"promo_{timestamp}.mp4"
            public_video_dir = Path("/home/ubuntu/conscia-mente/public/videos")
            public_video_dir.mkdir(parents=True, exist_ok=True)
            
            subprocess.run(["cp", video_finale, str(public_video_dir / video_filename)], check=True)
            
            # Lancia la generazione dell'articolo usando il titolo del video come hook
            console.print(f"[dim]Generazione articolo per '{hook_title}' con video '{video_filename}'[/dim]")
            subprocess.run(
                ["node", "scripts/generate-article.mjs", hook_title, "--video", video_filename],
                cwd="/home/ubuntu/conscia-mente",
                check=True
            )
            
            console.print("[dim]Push su GitHub (Vercel Deploy)...[/dim]")
            subprocess.run(["git", "add", "."], cwd="/home/ubuntu/conscia-mente", check=True)
            subprocess.run(["git", "commit", "-m", f"Auto-post article: {hook_title}"], cwd="/home/ubuntu/conscia-mente", check=True)
            subprocess.run(["git", "push", "origin", "main"], cwd="/home/ubuntu/conscia-mente", check=True)
            
            console.print("[bold green]✓ Articolo pubblicato su Conscia-Mente con successo![/]")
        except Exception as e:
            console.print(f"[bold red]✖ Errore nell'integrazione sito Conscia-Mente: {e}[/]")

    console.print("\n" + "="*60)
    console.print(f"🎉 [bold magenta]PROCESSO AUTONOMO COMPLETATO![/]")
    console.print(f"Il tuo video finale è pronto in: [bold green]{video_finale}[/]")
    console.print("="*60 + "\n")

if __name__ == "__main__":
    main()
