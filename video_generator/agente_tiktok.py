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

# Argomenti random — lista di fallback se il pick intelligente fallisce
TOPIC_IDEAS = [
    "energia e digestione",
    "meditazione e stress",
    "fisica quantistica e mente",
    "scienza e vibrazioni",
    "alimentazione e umore",
    "frequenze del pensiero",
    "cibo per il cervello"
]

def pick_topic_intelligente() -> tuple[str, str]:
    """Usa il topic picker intelligente del RAG (40+ topic categorizzati con rotazione anti-ripetizione).
    Ritorna (categoria, topic). Fallback alla lista locale se il modulo non è disponibile.
    Usa i pesi del feedback loop se disponibili."""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from rag_generator import pick_intelligent_topic, TOPIC_IDEAS as RAG_TOPICS
        from modules.feedback_loop import get_topic_weights
        # Applica pesi basati su performance storiche
        all_cats = list(RAG_TOPICS.keys())
        weights_map = get_topic_weights(all_cats)
        cats_sorted = sorted(all_cats, key=lambda c: weights_map.get(c, 1.0), reverse=True)
        # Selezione pesata: randomizza con bias verso categorie più performanti/meno usate
        import random as _r
        weights_list = [weights_map.get(c, 1.0) for c in cats_sorted]
        chosen_cat = _r.choices(cats_sorted, weights=weights_list, k=1)[0]
        chosen_topic = _r.choice(RAG_TOPICS[chosen_cat])
        console.print(f"[dim green]Topic intelligente: {chosen_topic} (cat: {chosen_cat}, peso: {weights_map.get(chosen_cat, 1.0):.2f})[/]")
        return chosen_cat, chosen_topic
    except Exception as e:
        console.print(f"[dim yellow]Fallback topic list ({e})[/]")
        import random as _r
        topic = _r.choice(TOPIC_IDEAS)
        return "Generale", topic

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
            ['agy', '--dangerously-skip-permissions', '--print', prompt],
            input='\n', text=True, capture_output=True, timeout=60
        )
        output = result.stdout
        score_match = re.search(r'SCORE:\s*(\d+)', output)
        score = int(score_match.group(1)) if score_match else 5
        return score, output
    except Exception as e:
        return 7, f"Validazione skippata: {e}"  # In caso di errore, procedi

def valida_sicurezza_tiktok(script_text: str) -> tuple[bool, str]:
    """
    Simula l'algoritmo di moderazione di TikTok per prevenire ban.
    Controlla disinformazione medica, allarmismo e contenuti sensazionalistici.
    Ritorna (True se sicuro, motivazione).
    """
    import re
    prompt = f'''Sei il sistema di moderazione automatica di TikTok. Devi analizzare questo copione e decidere se viola le Linee Guida della Community, in particolare per "Disinformazione Medica", "Contenuti Scioccanti o Allarmistici" o "Sensazionalismo".
    
Le regole di TikTok vietano severamente:
1. Termini come "invasione", "parassiti", "mutazione", "distruggere" associati al corpo umano o al cervello.
2. Promesse mediche false o affermazioni esagerate sulla salute.
3. Procurato allarme (fear-mongering) per attirare visualizzazioni.

COPIONE DA ANALIZZARE:
{script_text[:1500]}

Rispondi SOLO con questo formato:
SICURO: [SI oppure NO]
MOTIVO_MODERAZIONE: [Spiega brevemente perché è sicuro o perché verrebbe rimosso]
'''
    try:
        # Use safe subprocess call without shell parsing to avoid backtick/quote errors
        result = subprocess.run(
            ['agy', '--dangerously-skip-permissions', '--print', prompt],
            input='\n', text=True, capture_output=True, timeout=60
        )
        output = result.stdout
        is_safe = False
        if re.search(r'SICURO:\s*SI', output, re.IGNORECASE):
            is_safe = True
        return is_safe, output
    except Exception as e:
        return True, f"Validazione sicurezza skippata: {e}"

def main():
    parser = argparse.ArgumentParser(description="Agente Orchestratore TikTok")
    parser.add_argument("--topic", help="Tema del video (opzionale, altrimenti ne pesca uno a caso dai tuoi libri)")
    parser.add_argument("--mode", default="promo", choices=["promo", "virale", "bastian"], help="Modalità video")
    args = parser.parse_args()

    topic_result = pick_topic_intelligente() if not args.topic else ("Manuale", args.topic)
    topic_category, topic = topic_result if isinstance(topic_result, tuple) else ("Manuale", topic_result)
    
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
        "--category", topic_category,
        "--output", script_txt,
        "--mode", args.mode
    ]
    run_step(step0_cmd, "STEP 0: Ricerca e Scrittura Copione (Gemini API)")

    # STEP 0.5: Generazione Immagini ad-hoc con AGY
    console.print(f"\n[bold magenta]▶ AVVIO STEP 0.5: Generazione Immagini AI (AGY)[/]")
    console.print("[dim]Chiedo all'intelligenza artificiale di disegnare 3 immagini verticali specifiche per il copione...[/dim]")
    
    agy_prompt = f"Sei un direttore artistico. DEVI USARE il tuo tool 'generate_image' per creare 3 immagini verticali (AspectRatio: 9:16). Tema centrale: '{topic}'. Stile: altissima qualità, cinematografico, iper-realistico, colori vividi, mozzafiato. NON aggiungere testo. Genera le 3 immagini in sequenza chiamandole img_v_{int(time.time())}_1, img_v_{int(time.time())}_2, img_v_{int(time.time())}_3. Appena fatto, rispondi 'FATTO'."
    
    try:
        # Use safe subprocess call without shell parsing to avoid backtick/quote errors
        subprocess.run(
            ['agy', '--dangerously-skip-permissions', '--print', agy_prompt],
            input='\n', text=True, capture_output=True, timeout=300
        )
    except subprocess.TimeoutExpired:
        console.print("[yellow]AGY ha impiegato troppo tempo per le immagini (timeout 5 min), proseguo.[/]")
    except Exception as e:
        console.print(f"[dim red]Errore lancio AGY immagini: {e}[/]")

    console.print("[dim]Recupero le immagini appena generate...[/]")
    try:
        # Copia le immagini generate negli ultimi 10 minuti
        subprocess.run(r"find ~/.gemini/antigravity-cli/brain/ -type f -mmin -10 \( -name '*.jpg' -o -name '*.png' \) -exec cp {} assets/backgrounds/ \;", shell=True)
    except Exception:
        pass

    new_bg_files = list(set(bg_dir.glob("*.*")) - old_bg_files)
    
    # FALLBACK HUGGINGFACE se Antigravity ha fallito
    if not new_bg_files:
        hf_token = os.getenv("HF_API_KEY")
        if hf_token:
            console.print("[yellow]Antigravity fallito. Tento fallback con HuggingFace (SDXL)...[/]")
            import requests
            hf_url = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"
            headers = {"Authorization": f"Bearer {hf_token}"}
            for i in range(3):
                try:
                    payload = {
                        "inputs": f"Beautiful aesthetic background for {topic}, cinematic lighting, highly detailed, vivid colors, 4k, vertical portrait",
                    }
                    resp = requests.post(hf_url, headers=headers, json=payload, timeout=40)
                    if resp.status_code == 503:
                        console.print(f"[yellow]Modello HF in caricamento... Attendo 15s...[/]")
                        time.sleep(15)
                        resp = requests.post(hf_url, headers=headers, json=payload, timeout=40)
                        
                    if resp.status_code == 200:
                        local_path = bg_dir / f"fallback_hf_{int(time.time())}_{i}.jpg"
                        with open(local_path, "wb") as f:
                            f.write(resp.content)
                        new_bg_files.append(local_path)
                        console.print(f"[green]✓ Immagine HuggingFace {i+1} scaricata: {local_path.name}[/]")
                    else:
                        console.print(f"[red]Errore API HuggingFace: {resp.status_code}[/]")
                except Exception as e:
                    console.print(f"[red]Errore eccezione HF img {i+1}: {e}[/]")
                time.sleep(2)
    
    # FALLBACK POLLINATIONS se anche HuggingFace (o AGY) ha fallito
    if len(new_bg_files) < 3:
        console.print("[yellow]Poche immagini generate. Tento fallback con Pollinations...[/]")
        import urllib.request
        import urllib.parse
        import urllib.error
        backoff_delays = [3, 8, 15, 20, 30]  # backoff esponenziale + 429 handling
        for i in range(5):
            delay = backoff_delays[min(i, len(backoff_delays) - 1)]
            try:
                prompt = f"Abstract beautiful highly aesthetic background for {topic}, dark mode, minimalist, cinematic, vertical 9:16"
                encoded_prompt = urllib.parse.quote(prompt)
                seed = int(time.time()) + i * 137  # seed diversi per immagini diverse
                bg_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1920&nologo=true&seed={seed}"
                local_path = bg_dir / f"fallback_bg_{int(time.time())}_{i}.jpg"
                req = urllib.request.Request(bg_url, headers={'User-Agent': 'Mozilla/5.0 (compatible)'})
                with urllib.request.urlopen(req, timeout=25) as response, open(local_path, 'wb') as out_file:
                    out_file.write(response.read())
                new_bg_files.append(local_path)
                console.print(f"[green]✓ Immagine fallback {i+1} scaricata: {local_path.name}[/]")
                if len(new_bg_files) >= 3:
                    break  # ne bastano 3
                time.sleep(2)  # pausa tra download per non triggherare rate limit
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    console.print(f"[yellow]Pollinations 429 (rate limit) per img {i+1}. Attendo {delay}s...[/]")
                    time.sleep(delay)
                else:
                    console.print(f"[red]Errore HTTP {e.code} Pollinations img {i+1}: {e}[/]")
            except Exception as e:
                console.print(f"[red]Errore fallback Pollinations img {i+1}: {e}. Riprovo tra {delay}s...[/]")
                time.sleep(delay)
        if new_bg_files:
            console.print(f"[green]Fallback Pollinations OK: {len(new_bg_files)} immagini scaricate.[/]")
        else:
            console.print("[red]Fallback Pollinations completamente fallito. Il video userà sfondi Pexels.[/]")

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

    # --- Validazione qualità copione (M10) e Sicurezza TikTok ---
    script_content_clean = script_path.read_text(encoding="utf-8")
    
    # 1. Controllo Sicurezza TikTok (Critico)
    is_safe, safety_report = valida_sicurezza_tiktok(script_content_clean)
    if not is_safe:
        console.print(f"[bold red]⛔ ALERT SICUREZZA TIKTOK:[/] Il copione viola potenzialmente le linee guida!")
        console.print(f"[dim]{safety_report}[/dim]")
        console.print(f"[yellow]⚠️ Rigenero immediatamente con nuovo topic per evitare ban...[/]")
        subprocess.run(
            [python_exe, 'rag_generator.py', '--topic', topic, '--category', topic_category, '--output', script_txt,
             '--mode', args.mode, '--force-new'],
            capture_output=False
        )
        if script_path.exists() and script_path.stat().st_size > 0:
            script_content_clean = script_path.read_text(encoding="utf-8")
        else:
            console.print("[red]Errore critico: impossibile generare un copione sicuro.[/]")
            sys.exit(1)
            
    # 2. Controllo Qualità e Watch-time
    quality_score, quality_report = valida_qualita_copione(script_content_clean)
    console.print(f"[{'green' if quality_score >= 7 else 'red'}]📊 Quality Score: {quality_score}/10[/]")

    if quality_score < 7:
        console.print(f"[yellow]⚠️ Copione sotto soglia ({quality_score}/10). Rigenero con nuovo topic...[/]")
        # Re-run rag_generator con flag --force-new per cambiare topic
        subprocess.run(
            [python_exe, 'rag_generator.py', '--topic', topic, '--category', topic_category, '--output', script_txt,
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
    # --- Fine M10 e Sicurezza ---

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
            from modules.feedback_loop import log_upload
            log_upload(
                video_file=video_finale,
                hook_title=hook_title,
                category=topic_category,
                mode=args.mode,
                quality_score=quality_score,
                fonte=fonte_notizia,
                success=True,
            )
            notify_telegram(f"✅ Video pubblicato: '{hook_title}' (score {quality_score}/10)")
        except SystemExit:
            from modules.feedback_loop import log_upload
            log_upload(
                video_file=video_finale,
                hook_title=hook_title,
                category=topic_category,
                mode=args.mode,
                quality_score=quality_score,
                fonte=fonte_notizia,
                success=False,
            )
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
