import os
import argparse
from pathlib import Path
from rich.console import Console
import docx2txt
from google import genai
from dotenv import load_dotenv

load_dotenv()

console = Console()

# ── 1. Database degli eBook Reali (su disco esterno) ────────────────
EBOOKS_DIR = Path("/home/ubuntu/ebooks")

EBOOKS_DB = [
    {
        "titolo": "Come usare il cibo per correggere energia, umore e digestione",
        "file": EBOOKS_DIR / "Come usare il cibo - Edizione Ottimizzata.docx",
        "argomenti": ["cibo", "alimentazione", "energia", "umore", "digestione", "salute"],
        "pitch": "Scopri come usare il cibo per ritrovare energia e bilanciare il tuo umore leggendo il mio manuale."
    },
    {
        "titolo": "Meditazione per chiunque",
        "file": EBOOKS_DIR / "Meditazione per chiunque.docx",
        "argomenti": ["meditazione", "mente", "stress", "consapevolezza", "spiritualità", "pace"],
        "pitch": "Inizia a trasformare la tua mente oggi stesso con la guida pratica Meditazione per Chiunque."
    },
    {
        "titolo": "Integratori Naturali: Guida Scientifica",
        "file": EBOOKS_DIR / "INTEGRATORI NATURALI GUIDA SCIENTIFICA.docx",
        "argomenti": ["integratori", "scienza", "vitamine", "salute", "benessere", "corpo", "integrazione"],
        "pitch": "Scopri come supportare il tuo corpo al meglio leggendo la mia guida scientifica sugli integratori naturali."
    }
]

def find_best_ebook(topic: str) -> dict:
    """Trova l'ebook più inerente all'argomento del video."""
    topic_lower = topic.lower()
    for ebook in EBOOKS_DB:
        for arg in ebook["argomenti"]:
            if arg in topic_lower:
                return ebook
    
    # Fallback quando non ci sono argomenti pertinenti
    return {
        "titolo": "Generico",
        "file": None,
        "argomenti": [],
        "pitch": "Se ti interessano argomenti simili, fai un salto sul mio profilo."
    }

def estrai_testo_docx(file_path: Path) -> str:
    """Estrae il testo da un file .docx."""
    if file_path is None or not file_path.exists():
        if file_path is not None:
            console.print(f"[red]ATTENZIONE: File non trovato {file_path}[/]")
        return ""
    console.print(f"[dim]Lettura del documento {file_path.name}...[/]")
    testo = docx2txt.process(str(file_path))
    return testo

import asyncio
from google.antigravity import Agent, LocalAgentConfig, CapabilitiesConfig

def generate_tiktok_script(topic: str, ebook: dict, api_key: str, mode: str = "promo") -> str:
    """
    Usa Google Antigravity Agent per generare lo script TikTok e disegnare le immagini.
    L'agente elabora il testo in autonomia e ha i poteri per generare file grafici locali.
    """
    console.print(f"[cyan]Evocazione dell'Agente Antigravity sul tema:[/] {topic}")
    
    history_path = Path("used_news_history.txt")
    history = history_path.read_text(encoding="utf-8") if history_path.exists() else "Nessuna notizia usata finora"
    history = "\n".join(history.strip().split("\n")[-20:])
    
    # Prepara il prompt
    if mode == "promo":
        contenuto_libro = estrai_testo_docx(ebook["file"])
        prompt = f"""
        Sei un genio del copywriting persuasivo e sceneggiatore per un canale TikTok di divulgazione (stile documentario mozzafiato).
        Tema: {topic}. Testo da cui trarre ispirazione: {contenuto_libro[:30000]}
        
        CONCETTI GIA' TRATTATI RECENTEMENTE (EVITA ASSOLUTAMENTE DI RIPETERLI. Trova un angolo, capitolo o concetto NUOVO e DIVERSO):
        {history}
        
        Scrivi uno script TikTok di circa 130 parole in italiano che sia ESTREMAMENTE ORIGINALE, SENSATO E VIRALE.
        Non fare riassunti noiosi, ma estrai l'intuizione più sconvolgente o utile dal testo e raccontala come un segreto svelato.
        Scegli casualmente un dettaglio diverso per evitare di essere ripetitivo.
        
        REGOLE FONDAMENTALI PER LA VOCE (MOLTO IMPORTANTE):
        - Usa frasi molto brevi, ritmate e dal forte impatto emotivo.
        - Scrivi TUTTI i numeri in lettere (es. "cento" e non "100").
        - NON USARE MAI simboli speciali, parentesi, o virgolette.
        - Inserisci punti o virgole per far prendere fiato alla voce.
        
        STRUTTURA OBBLIGATORIA:
        - RIGA 1: Scrivi "TITOLO: " seguito da un titolo magnetico che generi forte curiosità (hook irresistibile).
        - RIGA 2 in poi: 
          1. HOOK IPNOTICO (primi 3 secondi): Usa leve psicologiche estreme (curiosità, paradosso, rottura di schemi). Inizia con una rivelazione scioccante o smontando una credenza comune per incollare l'utente allo schermo. Concentra il 90% dello sforzo sulle prime 15 parole.
          2. VALORE sensato, originale e concreto (spiega il "perché" in modo logico e affascinante).
          3. CTA finale organica: "{ebook['pitch']} Link in bio!".
        - ULTIMA RIGA (NUOVA E OBBLIGATORIA): Scrivi "FONTE_NOTIZIA: " seguito da un brevissimo riassunto di 1 riga del concetto esatto che hai trattato. Questo servirà al sistema per non fartelo ripetere in futuro!
          
        Inoltre, PRIMA di rispondere con il testo, usa il tuo tool per generare 3 IMMAGINI 9:16 diverse e altamente cinematografiche per il background. DOPO averle generate, USA IL TOOL run_command per spostarle nella cartella '/home/ubuntu/GIT/video_generator/assets/backgrounds/' con nomi come promo_bg_1.jpg, ecc.
        SE LA GENERAZIONE IMMAGINI FALLISCE PER LIMITE DI QUOTA O ERRORE 429: IGNORA il problema, NON scusarti e procedi a scrivere il copione senza menzionare l'accaduto.
        """
    elif mode == "virale":
        prompt = f"""
        Sei un maestro dello storytelling virale e analista brillante per TikTok. 
        
        TEMA OBBLIGATORIO: Qualsiasi argomento affascinante che stimoli estrema curiosità (es. esplorazione spaziale, storia nascosta, paradossi, biologia bizzarra, tecnologie rivoluzionarie, mente, psicologia, neuroscienze o fisica). Sii di ampie vedute.
        NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
        {history}
        
        STEP 1 (CRITICO): Usa il tool search_web per cercare su internet una notizia VERA, recente e altamente virale sul tema obbligatorio. Non usare conoscenze vecchie o argomenti già in lista, trova qualcosa di NUOVO e verifica la data.
        
        STEP 2: Scrivi un copione TikTok di circa 130 parole in italiano basato su questa notizia. Il copione deve essere SCONVOLGENTE, ORIGINALE ma profondamente SENSATO (basato sulla logica).
        Trova l'angolo più inaspettato della notizia. Perché dovrebbe importare a chi guarda? Come cambia la sua vita oggi?
        
        REGOLE FONDAMENTALI PER LA VOCE E IL TESTO:
        - Usa frasi incisive, brevi, come un dialogo intimo e rivelatore.
        - Scrivi TUTTI i numeri in lettere (es. "mille" e non "1000").
        - NON USARE MAI simboli speciali, parentesi, o virgolette.
        - Inserisci punti o virgole per far prendere fiato alla voce.
        
        STRUTTURA OBBLIGATORIA:
        - RIGA 1: "TITOLO: " + un titolo (max 5 parole). VIETATO usare frasi fatte come "scoperta assurda" o menzionare mesi (es. "ad agosto") per fingere attualità.
        - RIGA 2 in poi: 
          1. HOOK IPNOTICO (primi 3 secondi): Usa leve psicologiche estreme (curiosità, paradosso, rottura di schemi). Inizia con una rivelazione scioccante o smontando una credenza comune per incollare l'utente allo schermo. Concentra il 90% dello sforzo sulle prime 15 parole.
          2. STORIA/SPIEGAZIONE: cita sempre la FONTE a voce (es. "I ricercatori del MIT...").
          3. ENGAGEMENT potente: fai una domanda divisiva o profonda e chiedi la loro opinione nei commenti.
        - ULTIMA RIGA (NUOVA E OBBLIGATORIA): Scrivi "FONTE_NOTIZIA: " seguito da un brevissimo riassunto di 1 riga della vera notizia che hai trattato (es. Studio MIT sui topi e memoria). Questo servirà al sistema per non fartelo ripetere in futuro!
        
        CRITICO: PRIMA di scrivermi il copione, DEVI USARE il tuo strumento di generazione immagini per creare 3 IMMAGINI 9:16 fotorealistiche. Immediatamente dopo, USA IL TOOL run_command per copiarle/spostarle fisicamente in '/home/ubuntu/GIT/video_generator/assets/backgrounds/'. Solo dopo scrivimi il copione.
        SE LA GENERAZIONE IMMAGINI FALLISCE PER LIMITE DI QUOTA O ERRORE 429: IGNORA il problema, NON scusarti e procedi a scrivere il copione senza menzionare l'accaduto.
        """
    else: # mode == "bastian"
        prompt = f"""
        Sei un maestro dello storytelling virale e analista brillante per TikTok. 
        
        TEMA OBBLIGATORIO: Qualsiasi argomento affascinante che stimoli estrema curiosità (es. esplorazione spaziale, storia nascosta, paradossi, biologia bizzarra, tecnologie rivoluzionarie, mente, psicologia, neuroscienze o fisica). Sii di ampie vedute.
        NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
        {history}
        
        STEP 1 (CRITICO): Usa il tool search_web per cercare su internet una notizia VERA, recente e dibattuta sul tema obbligatorio. Non usare conoscenze vecchie o argomenti già in lista, trova qualcosa di NUOVO e verifica la data.
        
        STEP 2: Scrivi un copione TikTok di circa 130 parole in italiano usando la tecnica del "Bastian Contrario" (Angolo Contrariano).
        Analizza la notizia e proponi una visione fortemente impopolare, scomoda o contro-intuitiva, ma supportata da una logica ferrea (sensato).
        
        REGOLE FONDAMENTALI PER LA VOCE E IL TESTO:
        - Usa frasi incisive, brevi, come un dialogo intimo e rivelatore.
        - Scrivi TUTTI i numeri in lettere (es. "mille" e non "1000").
        - NON USARE MAI simboli speciali, parentesi, o virgolette.
        - Inserisci punti o virgole per far prendere fiato alla voce.
        
        STRUTTURA OBBLIGATORIA:
        - RIGA 1: "TITOLO: " + un titolo (max 5 parole). VIETATO usare frasi fatte come "scoperta assurda" o menzionare mesi (es. "ad agosto") per fingere attualità.
        - RIGA 2 in poi: 
          1. HOOK IPNOTICO (primi 3 secondi): Usa leve psicologiche estreme (curiosità, paradosso, rottura di schemi). Inizia con una rivelazione scioccante o smontando una credenza comune per incollare l'utente allo schermo. Concentra il 90% dello sforzo sulle prime 15 parole.
          2. STORIA/SPIEGAZIONE: cita sempre la FONTE a voce (es. "Secondo un recente studio di Stanford...").
          3. ENGAGEMENT potente: fai una domanda divisiva o profonda e chiedi la loro opinione nei commenti.
        - ULTIMA RIGA (NUOVA E OBBLIGATORIA): Scrivi "FONTE_NOTIZIA: " seguito da un brevissimo riassunto di 1 riga della vera notizia che hai trattato. Questo servirà al sistema per non fartelo ripetere in futuro!
        
        CRITICO: PRIMA di scrivermi il copione, DEVI USARE il tuo strumento di generazione immagini per creare 3 IMMAGINI 9:16 fotorealistiche. Immediatamente dopo, USA IL TOOL run_command per copiarle/spostarle fisicamente in '/home/ubuntu/GIT/video_generator/assets/backgrounds/'. Solo dopo scrivimi il copione.
        SE LA GENERAZIONE IMMAGINI FALLISCE PER LIMITE DI QUOTA O ERRORE 429: IGNORA il problema, NON scusarti e procedi a scrivere il copione senza menzionare l'accaduto.
        """

    console.print(f"[dim]Generazione del copione in corso tramite AGY CLI...[/]")
    
    try:
        import subprocess
        
        # Esecuzione tramite riga di comando AGY: usiamo --effort high e un modello pro per MASSIMA qualità
        result = subprocess.run(
            ["agy", "--dangerously-skip-permissions", "--print", prompt], 
            capture_output=True, 
            text=True
        )
        
        if result.returncode != 0:
            raise Exception(f"AGY CLI Error: {result.stderr}")
            
        script = result.stdout.strip()
        return script
    except Exception as e:
        console.print(f"[red]Errore Antigravity:[/] {e}")
        return "TITOLO: Errore di Generazione\nSiamo spiacenti, c'è stato un problema."

def main():
    parser = argparse.ArgumentParser(description="RAG Script Generator per TikTok (Gemini API)")
    parser.add_argument("--topic", required=True, help="Argomento (es. 'fisica quantistica', 'dna')")
    parser.add_argument("--output", default="scripts/script_generato.txt", help="Dove salvare lo script")
    parser.add_argument("--api-key", help="Gemini API Key (se non impostata come variabile d'ambiente GEMINI_API_KEY)")
    parser.add_argument("--mode", default="promo", choices=["promo", "virale", "bastian"], help="Modalità di generazione")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("GEMINI_API_KEY")

    # 1. Recupero (Retrieval) dell'ebook giusto
    ebook = find_best_ebook(args.topic)
    
    # 2. Generazione (Augmented Generation) con Gemini leggendo il DOCX
    script_text = generate_tiktok_script(args.topic, ebook, api_key, args.mode)
    
    # 3. Salvataggio
    out_path = Path(args.output)
    out_path.parent.mkdir(exist_ok=True, parents=True)
    out_path.write_text(script_text, encoding="utf-8")
    
    console.print(f"[bold green]✓ Script salvato in {out_path}[/]")
    console.print(f"Anteprima:\n[dim]{script_text}[/dim]")

if __name__ == "__main__":
    main()
