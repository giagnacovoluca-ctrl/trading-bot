import os
import sys
import time
import random
import argparse
import subprocess
import shutil
from pathlib import Path
from rich.console import Console

from modules.site_integration import parse_generated_manifest
from modules.script_quality import extract_metadata, validate_script
from modules.email_notifications import notify_email

console = Console()

AGY_VALIDATION_ATTEMPTS = 3
AGY_VALIDATION_BACKOFF_SECONDS = (5, 15)


def _run_agy_validator(prompt: str) -> subprocess.CompletedProcess:
    """Esegue un validatore AGY ritentando gli errori temporanei del servizio."""
    last_result = None
    for attempt in range(1, AGY_VALIDATION_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                ['agy', '--dangerously-skip-permissions', '--print', prompt],
                input='\n', text=True, capture_output=True, timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            result = subprocess.CompletedProcess(
                args=['agy'], returncode=1, stdout='', stderr=str(exc),
            )

        last_result = result
        if result.returncode == 0:
            return result

        if attempt < AGY_VALIDATION_ATTEMPTS:
            delay = AGY_VALIDATION_BACKOFF_SECONDS[attempt - 1]
            console.print(
                f"[yellow]Validatore AGY temporaneamente non disponibile "
                f"(tentativo {attempt}/{AGY_VALIDATION_ATTEMPTS}); "
                f"nuovo tentativo tra {delay}s.[/]"
            )
            time.sleep(delay)

    return last_result

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
        import random as _r
        sys.path.insert(0, str(Path(__file__).parent))
        from rag_generator import EDITORIAL_FAMILIES, TOPIC_IDEAS as RAG_TOPICS
        from modules.feedback_loop import get_recent_published, get_topic_weights
        # Applica pesi basati su performance storiche
        all_cats = list(RAG_TOPICS.keys())
        weights_map = get_topic_weights(all_cats)
        recent_published = get_recent_published(30)
        recent_categories = [e.get("category") for e in recent_published[-4:] if e.get("category")]
        recent_topics = {e.get("topic", "").strip().casefold() for e in recent_published if e.get("topic")}

        # Prima ruota equamente le famiglie editoriali. Così una famiglia con
        # molte sottocategorie scientifiche non domina quelle più piccole.
        recent_families = {
            EDITORIAL_FAMILIES.get(cat, cat) for cat in recent_categories[-2:]
        }
        all_families = list(dict.fromkeys(EDITORIAL_FAMILIES.get(cat, cat) for cat in all_cats))
        eligible_families = [family for family in all_families if family not in recent_families]
        if not eligible_families:
            eligible_families = all_families
        chosen_family = _r.choice(eligible_families)

        # Quattro pubblicazioni di distanza tra categorie, salvo esaurimento.
        eligible_cats = [
            cat for cat in all_cats
            if EDITORIAL_FAMILIES.get(cat, cat) == chosen_family
            and cat not in recent_categories
        ]
        if not eligible_cats:
            eligible_cats = [
                cat for cat in all_cats
                if EDITORIAL_FAMILIES.get(cat, cat) == chosen_family
            ]
        cats_sorted = sorted(eligible_cats, key=lambda c: weights_map.get(c, 1.0), reverse=True)

        # Selezione pesata con bias verso categorie performanti e non recenti.
        weights_list = [weights_map.get(c, 1.0) for c in cats_sorted]
        chosen_cat = _r.choices(cats_sorted, weights=weights_list, k=1)[0]
        eligible_topics = [t for t in RAG_TOPICS[chosen_cat] if t.casefold() not in recent_topics]
        if not eligible_topics:
            eligible_topics = RAG_TOPICS[chosen_cat]
        chosen_topic = _r.choice(eligible_topics)
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
        notify_email(f"ERRORE FATALE IN {step_name} (Exit code: {e.returncode})")
        sys.exit(1)

    durata = time.time() - start_time
    console.print(f"[bold green]✓ {step_name} COMPLETATO con successo in {durata:.1f} secondi.[/]\n")

def valida_qualita_copione(hook_title: str, script_text: str) -> tuple[int, str]:
    """
    Valida il copione con AGY prima della pubblicazione.
    Ritorna (score: int 0-10, motivazione: str).
    Score < 7 → rigenera con topic diverso.
    """
    import re
    prompt = f'''Sei un critico editoriale.
Valuta questo copione e il suo titolo con uno score da 0 a 10.
ATTENZIONE: Questo è un canale di divulgazione "soft". NON PENALIZZARE un tono calmo, discorsivo o leggermente accademico. È VIETATO pretendere titoli acchiappaclick o colpi di scena esagerati. Un hook semplice e pulito va benissimo.

Criteri:
- Qualità del Titolo (Hook): Il titolo è chiaro e introduce bene il tema? (peso 30%)
- Accuratezza scientifica: (peso 25%)
- Fluidità narrativa: (peso 25%)
- Originalità dell'aneddoto: (peso 20%)

TITOLO:
{hook_title}

COPIONE:
{script_text[:1500]}

Rispondi SOLO con questo formato:
SCORE: [numero da 0 a 10]
MOTIVAZIONE: [max 2 righe]
PROBLEMI: [lista bullet dei problemi principali, max 3]
'''

    try:
        result = _run_agy_validator(prompt)
        if result.returncode != 0:
            return 0, f"Validazione fallita: {result.stderr.strip()}"
        output = result.stdout
        return parse_quality_result(output)
    except Exception as e:
        return 0, f"Validazione fallita: {e}"


def parse_quality_result(output: str) -> tuple[int, str]:
    """Estrae score e diagnosi dalla risposta AGY senza interpretazioni ambigue."""
    import re
    score_match = re.search(r"^\s*SCORE\s*:\s*(\d{1,2})(?:\s*/\s*10)?\b", output or "", re.IGNORECASE | re.MULTILINE)
    score = max(0, min(10, int(score_match.group(1)))) if score_match else 5
    relevant = []
    for line in (output or "").splitlines():
        if re.match(r"\s*(MOTIVAZIONE|PROBLEMI)\s*:", line, re.IGNORECASE) or line.lstrip().startswith("-"):
            relevant.append(line.strip())
    return score, " ".join(relevant[:6])

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
        result = _run_agy_validator(prompt)
        if result.returncode != 0:
            return False, f"Validazione sicurezza fallita: {result.stderr.strip()}"
        output = result.stdout
        is_safe = False
        if re.search(r'SICURO:\s*SI', output, re.IGNORECASE):
            is_safe = True
        return is_safe, output
    except Exception as e:
        return False, f"Validazione sicurezza fallita: {e}"

def main():
    parser = argparse.ArgumentParser(description="Agente Orchestratore TikTok")
    parser.add_argument("--topic", help="Tema del video (opzionale, altrimenti ne pesca uno a caso dai tuoi libri)")
    parser.add_argument("--mode", default="promo", choices=["promo", "virale", "bastian"], help="Modalità video")
    parser.add_argument("--no-publish", action="store_true", help="Genera il video senza pubblicarlo sui social")
    parser.add_argument("--no-site", action="store_true", help="Non generare articolo o deploy su Conscia-Mente")
    args = parser.parse_args()

    ebook_id = ""
    if args.topic:
        topic_category, topic = "Manuale", args.topic
    elif args.mode == "promo":
        from rag_generator import pick_promo_ebook_topic
        topic_category, topic, ebook_id = pick_promo_ebook_topic()
    else:
        topic_result = pick_topic_intelligente()
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
    if ebook_id:
        step0_cmd.extend(["--ebook-id", ebook_id])
    run_step(step0_cmd, "STEP 0: Ricerca e Scrittura Copione (AGY CLI)")

    # Preflight prima delle immagini: non consumare Pollinations/Pexels se il
    # testo è già chiaramente sotto soglia. Il controllo completo viene
    # ripetuto più avanti dopo la normalizzazione dei metadati.
    preflight_raw = Path(script_txt).read_text(encoding="utf-8") if Path(script_txt).exists() else ""
    preflight_title_match = __import__("re").search(r"(?i)\**TITOLO:?\**\s*(.*?)(?=\n|$)", preflight_raw)
    preflight_title = preflight_title_match.group(1).replace("**", "").strip() if preflight_title_match else hook_title if 'hook_title' in locals() else ""
    preflight_text = __import__("re").sub(r"(?im)^\s*(?:FONTE_NOTIZIA|FATTO_CENTRALE|TIPO_EVIDENZA|LIMITE_EVIDENZA|ANGOLO_NARRATIVO):.*(?:\n|$)", "", preflight_raw)
    preflight_score, preflight_report = valida_qualita_copione(preflight_title, preflight_text)
    console.print(f"[{'green' if preflight_score >= 6 else 'red'}]📋 Preflight qualità: {preflight_score}/10[/]")
    if preflight_report:
        console.print(f"[dim]Diagnosi preflight: {preflight_report[:500]}[/]")
    if preflight_score == 0 and preflight_report.startswith("Validazione fallita"):
        sys.exit(74)
    if preflight_score < 6:
        console.print("[yellow]↻ Preflight insufficiente: scarto prima di generare immagini.[/]")
        sys.exit(75)
    preflight_safe, preflight_safety_report = valida_sicurezza_tiktok(preflight_text)
    if not preflight_safe:
        if preflight_safety_report.startswith("Validazione sicurezza fallita"):
            sys.exit(74)
        console.print("[yellow]↻ Preflight sicurezza insufficiente: scarto prima delle immagini.[/]")
        sys.exit(75)

    # Assicurati che lo script esista e non sia vuoto
    script_path = Path(script_txt)
    if not script_path.exists() or script_path.stat().st_size == 0:
        console.print("[red]Errore: Lo script non è stato generato o è vuoto.[/]")
        sys.exit(1)

    # Estrai il titolo, la fonte e pulisci lo script per la voce
    def extract_and_clean_script(script_text):
        h_title = "Scoperta Interessante"
        f_notizia = ""
        e_file = ""

        import re

        # Estrai TITOLO (handling "TITOLO: " anche in mezzo alla riga)
        t_match = re.search(r'(?i)\**TITOLO:?\**\s*(.*?)(?=\n|$)', script_text)
        if t_match:
            h_title = t_match.group(1).replace('**', '').replace('"', '').strip()
            # Rimuovi dal testo
            script_text = script_text[:t_match.start()] + script_text[t_match.end():]

        # Estrai FONTE_NOTIZIA
        f_match = re.search(r'(?i)\**FONTE_NOTIZIA:?\**\s*(.*?)(?=\n|$)', script_text)
        if f_match:
            f_notizia = f_match.group(1).replace('**', '').strip()
            script_text = script_text[:f_match.start()] + script_text[f_match.end():]

        # Estrai EBOOK_FILE
        e_match = re.search(r'(?i)\**EBOOK_FILE:?\**\s*(.*?)(?=\n|$)', script_text)
        if e_match:
            e_file = e_match.group(1).replace('**', '').strip()
            script_text = script_text[:e_match.start()] + script_text[e_match.end():]

        # Rimuovi eventuale "TESTO:" rimasto
        script_text = re.sub(r'(?im)^\s*\**TESTO:?\**\s*', '', script_text)

        # I metadati editoriali restano nel file per il controllo qualità, ma
        # non devono mai essere letti dalla voce sintetica.
        for field in ("FATTO_CENTRALE", "TIPO_EVIDENZA", "LIMITE_EVIDENZA", "ANGOLO_NARRATIVO"):
            script_text = re.sub(rf'(?im)^\s*{field}:.*(?:\n|$)', '', script_text)

        c_lines = []
        for line in script_text.splitlines():
            # Rimuovi etichette ATTO
            cleaned_line = re.sub(r'(?i)^\s*(#*\s*\**ATTO[^:]+:\**\s*)', '', line)
            cleaned_line = re.sub(r'(?i)^\s*(#*\s*\**ATTO[^-]+-\**\s*)', '', cleaned_line)
            if cleaned_line.strip():
                c_lines.append(cleaned_line.strip())

        if not h_title or h_title == "Scoperta Interessante":
            full_txt = " ".join(c_lines)
            if full_txt:
                h_title = " ".join(full_txt.split()[:5])

        return h_title, f_notizia, e_file, "\n".join(c_lines).strip()

    script_content = script_path.read_text(encoding="utf-8")
    editorial_metadata = extract_metadata(script_content)
    hook_title, fonte_notizia, ebook_filename, script_content_clean = extract_and_clean_script(script_content)

    if "Errore" in hook_title or hook_title == "SCOPERTA SHOCK":
        notify_email(f"ERRORE FATALE: hook_title invalido ({hook_title})")
        console.print(f"[bold red]✖ ERRORE FATALE:[/] hook_title invalido ({hook_title}). Processo interrotto per evitare la pubblicazione del video errato.")
        sys.exit(1)

    # Sovrascrivi il file pulito senza i metadati (così la voce non li legge)
    script_path.write_text(script_content_clean, encoding="utf-8")

    # --- Validazione qualità copione (M10) e Sicurezza TikTok ---

    # 1. Controllo Sicurezza TikTok (Critico)
    is_safe, safety_report = valida_sicurezza_tiktok(script_content_clean)
    if not is_safe:
        console.print(f"[bold red]⛔ ALERT SICUREZZA TIKTOK:[/] Il copione viola potenzialmente le linee guida!")
        console.print(f"[dim]{safety_report}[/dim]")
        console.print(f"[yellow]⚠️ Rigenero immediatamente con nuovo topic per evitare ban...[/]")
        regeneration_cmd = [
            python_exe, 'rag_generator.py', '--topic', topic,
            '--category', topic_category, '--output', script_txt,
            '--mode', args.mode, '--force-new',
        ]
        if ebook_id:
            regeneration_cmd.extend(['--ebook-id', ebook_id])
        subprocess.run(
            regeneration_cmd,
            capture_output=False,
            check=True,
        )
        if script_path.exists() and script_path.stat().st_size > 0:
            script_content = script_path.read_text(encoding="utf-8")
            editorial_metadata = extract_metadata(script_content)
            hook_title, fonte_notizia, ebook_filename, script_content_clean = extract_and_clean_script(script_content)
            script_path.write_text(script_content_clean, encoding="utf-8")
            is_safe, safety_report = valida_sicurezza_tiktok(script_content_clean)
            if not is_safe:
                notify_email("ERRORE: anche il secondo copione non supera la validazione sicurezza")
                console.print(f"[bold red]⛔ Secondo copione non sicuro:[/] {safety_report}")
                sys.exit(1)
        else:
            console.print("[red]Errore critico: impossibile generare un copione sicuro.[/]")
            sys.exit(1)

    # 2. Controllo Qualità e Watch-time
    quality_score, quality_report = valida_qualita_copione(hook_title, script_content_clean)
    if quality_score == 0 and quality_report.startswith("Validazione fallita"):
        console.print("[yellow]↻ Valutatore non disponibile: riprovo il job senza pubblicare.[/]")
        sys.exit(74)
    console.print(f"[{'green' if quality_score >= 6 else 'red'}]📊 Quality Score: {quality_score}/10[/]")
    if quality_report:
        console.print(f"[dim]Diagnosi valutatore: {quality_report[:500]}[/]")

    if quality_score < 6:
        console.print(f"[yellow]⚠️ Copione o titolo sotto soglia ({quality_score}/10). Rigenero un nuovo copione per lo stesso argomento...[/]")
        # Re-run rag_generator per generare un testo migliore sullo STESSO topic
        regeneration_cmd = [
            python_exe, 'rag_generator.py', '--topic', topic,
            '--category', topic_category, '--output', script_txt,
            '--mode', args.mode, '--force-new',
        ]
        if ebook_id:
            regeneration_cmd.extend(['--ebook-id', ebook_id])
        subprocess.run(
            regeneration_cmd,
            capture_output=False,
            check=True,
        )
        # Ricarica e ripulisci il copione rigenerato
        if script_path.exists() and script_path.stat().st_size > 0:
            script_content = script_path.read_text(encoding="utf-8")
            editorial_metadata = extract_metadata(script_content)
            hook_title, fonte_notizia, ebook_filename, script_content_clean = extract_and_clean_script(script_content)
            script_path.write_text(script_content_clean, encoding="utf-8")
            is_safe, safety_report = valida_sicurezza_tiktok(script_content_clean)
            if not is_safe:
                notify_email("ERRORE: copione rigenerato per qualità non supera la sicurezza")
                console.print(f"[bold red]⛔ Copione rigenerato non sicuro:[/] {safety_report}")
                sys.exit(1)

            quality_score, quality_report = valida_qualita_copione(hook_title, script_content_clean)
            if quality_score == 0 and quality_report.startswith("Validazione fallita"):
                console.print("[yellow]↻ Valutatore non disponibile nel retry: riprovo il job.[/]")
                sys.exit(74)
            console.print(f"[cyan]📊 Quality Score (secondo tentativo): {quality_score}/10[/]")
            if quality_report:
                console.print(f"[dim]Diagnosi valutatore: {quality_report[:500]}[/]")
            if quality_score < 6:
                console.print(
                    "[bold yellow]↻ Copione scartato: qualità ancora sotto soglia. "
                    "L'orchestratore ripartirà con un nuovo argomento.[/]"
                )
                # EX_TEMPFAIL: distingue un contenuto da rigenerare da un vero
                # errore tecnico. run_agent_until_publish.sh intercetta il 75.
                sys.exit(75)
        else:
            console.print("[red]Rigenero fallito: uso il copione originale.[/]")
    # --- Fine M10 e Sicurezza ---

    local_quality = validate_script(hook_title, script_content_clean, editorial_metadata)
    if not local_quality.ok:
        details = "; ".join(local_quality.issues)
        console.print(f"[bold yellow]↻ Contenuto scartato dal controllo editoriale:[/] {details}")
        sys.exit(75)
    console.print(f"[green]✓ Controllo editoriale locale: {local_quality.score}/10[/]")

    # Salva la vera fonte nello storico per non ripetere la notizia
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

    # STEP 0.5: Download Immagini (Pexels / Pollinations Fallback)
    console.print(f"\n[bold magenta]▶ AVVIO STEP 0.5: Download Immagini[/]")

    new_bg_files = []
    if os.getenv("PEXELS_API_KEY"):
        try:
            console.print("[dim]Cerco immagini su Pexels...[/dim]")
            import urllib.request
            import urllib.parse
            import json

            headers = {"Authorization": os.getenv("PEXELS_API_KEY")}
            stop_words = {"e", "a", "il", "la", "le", "lo", "gli", "i", "un", "uno", "una", "di", "da", "in", "con", "su", "per"}
            topic_words = [w for w in topic.split() if w.lower() not in stop_words]
            search_query = " ".join(topic_words) if topic_words else "aesthetic"

            url = f"https://api.pexels.com/v1/search?query={urllib.parse.quote(search_query)}&per_page=6&orientation=portrait&size=large"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read())

            photos = data.get("photos", [])
            for i, p in enumerate(photos[:3]):
                img_url = p["src"]["large2x"]
                local_path = bg_dir / f"pexels_img_{int(time.time())}_{i}.jpg"

                req_img = urllib.request.Request(img_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req_img, timeout=10) as img_resp, open(local_path, 'wb') as out_file:
                    out_file.write(img_resp.read())
                new_bg_files.append(local_path)
                console.print(f"[green]✓ Immagine {i+1} scaricata da Pexels[/]")
        except Exception as e:
            error_code = getattr(e, "code", None)
            if error_code in (401, 403):
                # Evita ulteriori chiamate Pexels nello step 2 dello stesso run.
                os.environ["PEXELS_DISABLED"] = "1"
                console.print(
                    "[yellow]Pexels ha rifiutato la chiave (HTTP "
                    f"{error_code}); lo disabilito per questo run e passo subito al fallback.[/]"
                )
            else:
                console.print(f"[red]Errore Pexels images: {e}[/]")
    else:
        console.print("[yellow]PEXELS_API_KEY mancante, passo a Pollinations.[/]")

    # FALLBACK SU POLLINATIONS SE PEXELS FALLISCE O MANCANO IMMAGINI
    if len(new_bg_files) < 3:
        console.print("[yellow]⚠️ Uso Pollinations AI per generare immagini mancanti...[/]")
        import urllib.parse
        import urllib.request
        for i in range(len(new_bg_files), 3):
            prompt = f"Abstract atmospheric vertical background for {topic}, cinematic lighting, highly detailed, minimalist"
            pollinations_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?width=1080&height=1920&model=flux&enhance=true&nologo=true&private=true&seed={int(time.time())+i}"
            local_path = bg_dir / f"pollinations_img_{int(time.time())}_{i}.jpg"

            retry_delays = (0, 5, 12)
            for attempt, retry_delay in enumerate(retry_delays, start=1):
                try:
                    if retry_delay:
                        time.sleep(retry_delay)
                    req_img = urllib.request.Request(pollinations_url, headers={'User-Agent': 'Mozilla/5.0'})
                    partial_path = local_path.with_suffix(".part")
                    with urllib.request.urlopen(req_img, timeout=45) as img_resp, open(partial_path, 'wb') as out_file:
                        out_file.write(img_resp.read())
                    if partial_path.stat().st_size < 10_000:
                        raise ValueError("risposta immagine troppo piccola")
                    partial_path.replace(local_path)
                    new_bg_files.append(local_path)
                    console.print(f"[green]✓ Immagine {i+1} generata con Pollinations AI[/]")
                    time.sleep(2) # Pausa breve per evitare HTTP 429
                    break
                except Exception as e:
                    partial_path = local_path.with_suffix(".part")
                    if partial_path.exists():
                        partial_path.unlink()
                    console.print(f"[yellow]Tentativo {attempt}/3 Pollinations fallito: {e}[/]")
            else:
                console.print(f"[red]Errore definitivo Pollinations per immagine {i+1}[/]")

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
    try:
        run_step(step1_cmd, f"STEP 1: Generazione Voce Clonate Locale ({provider.upper()})")
        if not Path(audio_mp3).exists() or Path(audio_mp3).stat().st_size < 1000:
            raise SystemExit(1)
    except SystemExit:
        console.print("[yellow]⚠️ Voce fallita o corrotta. Tento fallback con Edge-TTS...[/]")
        step1_fallback = [python_exe, "step1_voce.py", "--script", script_txt, "--output", audio_mp3, "--provider", "edge-tts"]
        run_step(step1_fallback, "STEP 1: Fallback Voce (EDGE-TTS)")

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

    try:
        run_step(step2_cmd, "STEP 2: Download Sfondo e Montaggio")
        if not Path(video_base).exists() or Path(video_base).stat().st_size < 10000:
            raise SystemExit(1)
    except SystemExit:
        console.print("[yellow]⚠️ Sfondo fallito o corrotto. Tento fallback senza immagini custom...[/]")
        step2_fallback = [python_exe, "step2_sfondo.py", "--audio", audio_mp3, "--output", video_base, "--topic", topic]
        run_step(step2_fallback, "STEP 2: Fallback Sfondo")

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

    try:
        run_step(step3_cmd, "STEP 3: Sottotitoli Dinamici e CTA (Whisper)")
        if not Path(video_finale).exists() or Path(video_finale).stat().st_size < 50000:
            raise SystemExit(1)
    except SystemExit:
        console.print("[yellow]⚠️ Sottotitoli falliti o corrotti. Riprovo in fallback base...[/]")
        step3_fallback = [python_exe, "step3_sottotitoli.py", "--video", video_base, "--audio", audio_mp3, "--output", video_finale]
        run_step(step3_fallback, "STEP 3: Fallback Sottotitoli")

    # STEP 4: Pubblicazione Automatica (Opzionale)
    resource_data = None
    if ebook_id:
        from modules.ebook_catalog import get_ebook
        resource_data = get_ebook(ebook_id)
    profile_path = Path("chrome_profile")
    cookies_path = Path("cookies.txt")
    if args.no_publish:
        console.print("\n[bold yellow]Modalità --no-publish: upload social disabilitato.[/]")
    elif profile_path.exists() or cookies_path.exists():
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
                topic=topic,
                platform="tiktok",
                resource_id=ebook_id,
                delivery_type=resource_data["deliveryType"] if resource_data else "",
            )
            from rag_generator import save_published_history
            save_published_history(
                category=topic_category,
                topic=topic,
                fonte=fonte_notizia,
                metadata=editorial_metadata,
            )
            notify_email(f"✅ Video pubblicato: '{hook_title}' (score {quality_score}/10)")
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
                topic=topic,
                platform="tiktok",
                resource_id=ebook_id,
                delivery_type=resource_data["deliveryType"] if resource_data else "",
            )
            raise
    else:
        console.print("\n[dim]Salto la pubblicazione automatica: né 'chrome_profile' né 'cookies.txt' trovati.[/]")

    # --- INTEGRAZIONE SITO NEXT.JS (Solo in modalità promo) ---
    if args.mode == "promo" and not args.no_site:
        copied_video = None
        site_commit_created = False
        try:
            console.print(f"\n[bold cyan]🔗 INTEGRAZIONE SITO: Avvio auto-post articolo su Conscia-Mente...[/]")

            # Copia il video nella directory di Conscia-Mente
            promo_video_name = f"promo_{int(time.time())}.mp4"
            public_video_dir = Path("/home/ubuntu/conscia-mente/public/videos")
            public_video_dir.mkdir(parents=True, exist_ok=True)

            copied_video = public_video_dir / promo_video_name
            shutil.copy2(video_finale, copied_video)
            console.print(f"Generazione articolo per '{hook_title}' con video '{promo_video_name}'")

            # 1. Avvia lo script di generazione articolo (passando il file video se lo script lo supporta)
            # Assicuriamoci di essere nella cartella conscia-mente per eseguire npm/node
            blog_cmd = ["node", "scripts/generate-article.mjs", hook_title, "--video", f"videos/{promo_video_name}"]
            if ebook_filename:
                blog_cmd.extend(["--ebook", ebook_filename])

            blog_result = subprocess.run(
                blog_cmd,
                cwd="/home/ubuntu/conscia-mente",
                check=True,
                capture_output=True,
                text=True,
            )
            console.print(blog_result.stdout)
            generated_files = parse_generated_manifest(
                blog_result.stdout, Path("/home/ubuntu/conscia-mente")
            )

            # Push automatico su Vercel
            console.print("[dim]Push su GitHub (Vercel Deploy)...[/dim]")
            files_to_commit = [f"public/videos/{promo_video_name}", *generated_files]
            subprocess.run(["git", "config", "user.email", "giagnacovo.luca@gmail.com"], cwd="/home/ubuntu/conscia-mente", check=True)
            subprocess.run(["git", "config", "user.name", "Luca"], cwd="/home/ubuntu/conscia-mente", check=True)
            subprocess.run(["git", "add", "--", *files_to_commit], cwd="/home/ubuntu/conscia-mente", check=True)
            subprocess.run(["git", "commit", "-m", f"Auto-post article: {hook_title}"], cwd="/home/ubuntu/conscia-mente", check=True)
            site_commit_created = True
            subprocess.run(["git", "push", "origin", "main"], cwd="/home/ubuntu/conscia-mente", check=True)

            console.print("[bold green]✓ Articolo pubblicato su Conscia-Mente con successo![/]")
        except Exception as e:
            console.print(f"[bold red]✖ Errore nell'integrazione sito Conscia-Mente: {e}[/]")
            if copied_video and copied_video.exists() and not site_commit_created:
                copied_video.unlink()
            notify_email(f"ERRORE integrazione Conscia-Mente: {e}")
            raise SystemExit(1) from e

    console.print("\n" + "="*60)
    console.print(f"🎉 [bold magenta]PROCESSO COMPLETATO![/]")
    console.print(f"Il tuo video finale è pronto in: [bold green]{video_finale}[/]")
    console.print("="*60 + "\n")

if __name__ == "__main__":
    main()
