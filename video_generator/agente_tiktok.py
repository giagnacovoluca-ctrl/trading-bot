import os
import sys
import time
import random
import argparse
import subprocess
import requests
from pathlib import Path
from rich.console import Console

console = Console()

def notify_telegram(message: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        console.print("[dim yellow]Telegram notify skipped (missing env variables)[/]")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": message})
    except Exception as e:
        console.print(f"[dim red]Errore notifica Telegram: {e}[/]")

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
        notify_telegram(f"ERRORE FATALE IN {step_name} (Exit code: {e.returncode})")
        sys.exit(1)
        
    durata = time.time() - start_time
    console.print(f"[bold green]✓ {step_name} COMPLETATO con successo in {durata:.1f} secondi.[/]\n")

def valida_qualita_copione(script_text: str) -> tuple[int, str]:
    """
    Valida il copione con AGY prima della pubblicazione.
    Ritorna (score: int 0-10, motivazione: str).
    Score < 7 → rigenera con topic diverso.
    """
    import re
    prompt = f'''Sei un critico editoriale esperto di TikTok e contenuti scientifici virali.
Valuta questo copione con uno score da 0 a 10 basandoti su:
- Accuratezza scientifica (no esagerazioni, no trasformazioni correlazione→causalità): peso 40%
- Potenziale watch-time (hook forte, struttura narrativa, colpo di scena): peso 35%  
- Originalità (evita cliché, argomento fresco, non ripetitivo): peso 25%

COPIONE:
{script_text[:1500]}

Rispondi SOLO con questo formato:
SCORE: [numero da 0 a 10]
MOTIVAZIONE: [max 2 righe]
PROBLEMI: [lista bullet dei problemi principali, max 3]
'''

    try:
        result = subprocess.run(
            ['agy', 'run', '--model', 'flash', '--prompt', prompt],
            capture_output=True, text=True, timeout=60
        )
        output = result.stdout
        score_match = re.search(r'SCORE:\s*(\d+)', output)
        score = int(score_match.group(1)) if score_match else 5
        return score, output
    except Exception as e:
        return 7, f"Validazione skippata: {e}"  # In caso di errore, procedi

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
    for f in [script_txt, audio_mp3, video_base, "scripts/tiktok_caption.txt"]:
        if Path(f).exists():
            Path(f).unlink()

    bg_dir = Path("assets/backgrounds")
    bg_dir.mkdir(parents=True, exist_ok=True)
    
    # Pulizia bg generati
    now = time.time()
    for f in list(bg_dir.glob("*.*")):
        name = f.name.lower()
        if name.startswith("fallback_bg_") or name.startswith("pexels_"):
            f.unlink()
        elif f.suffix.lower() in [".jpg", ".png", ".jpeg"]:
            if now - f.stat().st_mtime < 7200:
                f.unlink()

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
        # Copia i media generati dall'agente
        subprocess.run(r"find ~/.gemini/antigravity-cli/brain/ -type f -mmin -5 \( -name '*.jpg' -o -name '*.png' \) -exec cp {} assets/backgrounds/ \;", shell=True)
    except Exception:
        pass

    new_bg_files = list(set(bg_dir.glob("*.*")) - old_bg_files)
    
    # FALLBACK POLLINATIONS se Antigravity ha fallito
    if not new_bg_files:
        console.print("[yellow]Antigravity non ha generato immagini. Tento fallback con Pollinations...[/]")
        import urllib.request
        import urllib.parse
        import time
        for i in range(3):
            try:
                prompt = f"Abstract beautiful highly aesthetic background for {topic}, dark mode, minimalist, vertical 9:16"
                encoded_prompt = urllib.parse.quote(prompt)
                bg_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1920&nologo=true&seed={int(time.time())+i}"
                local_path = bg_dir / f"fallback_bg_{int(time.time())}_{i}.jpg"
                req = urllib.request.Request(bg_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=10) as response, open(local_path, 'wb') as out_file:
                    out_file.write(response.read())
                new_bg_files.append(local_path)
                console.print(f"[dim]Scarico immagine fallback {i+1}/3...[/]")
            except Exception as e:
                console.print(f"[red]Errore fallback Pollinations slide {i}: {e}[/]")
        if new_bg_files:
            console.print("[green]Fallback Pollinations riuscito per i video![/]")

    # Assicurati che lo script esista e non sia vuoto
    script_path = Path(script_txt)
    if not script_path.exists() or script_path.stat().st_size == 0:
        console.print("[red]Errore: Lo script non è stato generato o è vuoto.[/]")
        sys.exit(1)
        
    # Estrai il titolo, la fonte e pulisci lo script per la voce
    script_content = script_path.read_text(encoding="utf-8").splitlines()
    hook_title = "Scoperta Interessante"
    fonte_notizia = ""
    ebook_filename = ""
    clean_lines = []
    for line in script_content:
        if line.startswith("TITOLO:"):
            hook_title = line.replace("TITOLO:", "").strip()
        elif line.startswith("FONTE_NOTIZIA:"):
            fonte_notizia = line.replace("FONTE_NOTIZIA:", "").strip()
        elif line.startswith("EBOOK_FILE:"):
            ebook_filename = line.replace("EBOOK_FILE:", "").strip()
        elif line.startswith("TESTO:"):
            clean_lines.append(line.replace("TESTO:", "").strip())
        else:
            clean_lines.append(line)
            
    if not hook_title or hook_title == "Scoperta Interessante":
        full_text = " ".join(clean_lines)
        if full_text:
            hook_title = " ".join(full_text.split()[:5])

    if "Errore" in hook_title or hook_title == "SCOPERTA SHOCK":
        notify_telegram(f"ERRORE FATALE: hook_title invalido ({hook_title})")
        console.print(f"[bold red]✖ ERRORE FATALE:[/] hook_title invalido ({hook_title}). Processo interrotto per evitare la pubblicazione del video errato.")
        sys.exit(1)
            
    # Sovrascrivi il file pulito senza i metadati (così la voce non li legge)
    script_path.write_text("\n".join(clean_lines).strip(), encoding="utf-8")

    # --- Validazione qualità copione (M10) ---
    script_content_clean = script_path.read_text(encoding="utf-8")
    quality_score, quality_report = valida_qualita_copione(script_content_clean)
    console.print(f"[{'green' if quality_score >= 7 else 'red'}]📊 Quality Score: {quality_score}/10[/]")

    if quality_score < 7:
        console.print(f"[yellow]⚠️ Copione sotto soglia ({quality_score}/10). Rigenero con nuovo topic...[/]")
        # Re-run rag_generator con flag --force-new per cambiare topic
        subprocess.run(
            [python_exe, 'rag_generator.py', '--topic', topic, '--output', script_txt,
             '--mode', args.mode, '--force-new'],
            capture_output=False
        )
        # Ricarica e ripulisci il copione rigenerato
        if script_path.exists() and script_path.stat().st_size > 0:
            script_content_clean = script_path.read_text(encoding="utf-8")
            quality_score, quality_report = valida_qualita_copione(script_content_clean)
            console.print(f"[cyan]📊 Quality Score (secondo tentativo): {quality_score}/10[/]")
        else:
            console.print("[red]Rigenero fallito: uso il copione originale.[/]")
    # --- Fine M10 ---

    # Salva la vera fonte nello storico per non ripetere la notizia
    if fonte_notizia and "Errore" not in fonte_notizia:
        with open("used_news_history.txt", "a", encoding="utf-8") as f:
            f.write(fonte_notizia + "\n")
            
    if Path("used_news_history.txt").exists():
        history_lines = Path("used_news_history.txt").read_text(encoding="utf-8").splitlines()
        filtered = [l for l in history_lines if "SCOPERTA ASSURDA" not in l and "Errore" not in l and "SCOPERTA SHOCK" not in l]
        Path("used_news_history.txt").write_text("\n".join(filtered) + "\n", encoding="utf-8")
    
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
    profile_path = Path("chrome_profile")
    cookies_path = Path("cookies.txt")
    if profile_path.exists() or cookies_path.exists():
        console.print("\n[bold magenta]🚀 Profilo/Cookie trovati: Avvio pubblicazione automatica su TikTok...[/]")
        step4_cmd = [
            python_exe, "step4_pubblica.py", 
            "--video", video_finale, 
            "--script", script_txt,
            "--mode", args.mode
        ]
        if cookies_path.exists() and not profile_path.exists():
            step4_cmd.extend(["--cookies", str(cookies_path)])
            
        try:
            run_step(step4_cmd, "STEP 4: Generazione Metadata e Upload TikTok")
            import json
            import datetime
            Path("output").mkdir(exist_ok=True)
            with open("output/upload_log.json", "a", encoding="utf-8") as f:
                json.dump({
                    "timestamp": datetime.datetime.now().isoformat(),
                    "video_file": video_finale,
                    "mode": args.mode,
                    "hook_title": hook_title,
                    "fonte_notizia": fonte_notizia,
                    "quality_score": quality_score,
                    "success": True
                }, f)
                f.write("\n")
        except SystemExit as e:
            import json
            import datetime
            Path("output").mkdir(exist_ok=True)
            with open("output/upload_log.json", "a", encoding="utf-8") as f:
                json.dump({
                    "timestamp": datetime.datetime.now().isoformat(),
                    "video_file": video_finale,
                    "mode": args.mode,
                    "hook_title": hook_title,
                    "fonte_notizia": fonte_notizia,
                    "quality_score": quality_score,
                    "success": False,
                    "error": "Errore durante upload TikTok"
                }, f)
                f.write("\n")
            raise
    else:
        console.print("\n[dim]Salto la pubblicazione automatica: né 'chrome_profile' né 'cookies.txt' trovati.[/]")

    # --- INTEGRAZIONE SITO NEXT.JS (Solo in modalità promo) ---
    if args.mode == "promo":
        try:
            console.print(f"\n[bold cyan]🔗 INTEGRAZIONE SITO: Avvio auto-post articolo su Conscia-Mente...[/]")
            
            # Copia il video nella directory di Conscia-Mente
            promo_video_name = f"promo_{int(time.time())}.mp4"
            public_video_dir = Path("/home/ubuntu/conscia-mente/public/videos")
            public_video_dir.mkdir(parents=True, exist_ok=True)
            
            subprocess.run(["cp", str(video_finale), str(public_video_dir / promo_video_name)], check=True)
            console.print(f"Generazione articolo per '{hook_title}' con video '{promo_video_name}'")
            
            # 1. Avvia lo script di generazione articolo (passando il file video se lo script lo supporta)
            # Assicuriamoci di essere nella cartella conscia-mente per eseguire npm/node
            blog_cmd = ["node", "scripts/generate-article.mjs", "--topic", hook_title, "--video", f"videos/{promo_video_name}"]
            if ebook_filename:
                blog_cmd.extend(["--ebook", ebook_filename])
                
            subprocess.run(
                blog_cmd, 
                cwd="/home/ubuntu/conscia-mente",
                check=True
            )
            
            # Push automatico su Vercel
            console.print("[dim]Push su GitHub (Vercel Deploy)...[/dim]")
            subprocess.run(["git", "add", "."], cwd="/home/ubuntu/conscia-mente", check=True)
            subprocess.run(["git", "commit", "-m", f"Auto-post article: {hook_title}"], cwd="/home/ubuntu/conscia-mente", check=True)
            subprocess.run(["git", "push", "origin", "main"], cwd="/home/ubuntu/conscia-mente", check=True)
            
            console.print("[bold green]✓ Articolo pubblicato su Conscia-Mente con successo![/]")
        except Exception as e:
            console.print(f"[bold red]✖ Errore nell'integrazione sito Conscia-Mente: {e}[/]")

    console.print("\n" + "="*60)
    console.print(f"🎉 [bold magenta]PROCESSO COMPLETATO![/]")
    console.print(f"Il tuo video finale è pronto in: [bold green]{video_finale}[/]")
    console.print("="*60 + "\n")

if __name__ == "__main__":
    main()
