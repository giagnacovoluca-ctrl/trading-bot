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
EBOOKS_DIR = Path("/run/media/magic/72A7-38D3/ebook")

EBOOKS_DB = [
    {
        "titolo": "Come usare il cibo per correggere energia, umore e digestione",
        "file": EBOOKS_DIR / "Come usare il cibo per correggere energia, umore e digestione- V2 (3).docx",
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
        "titolo": "Tra Scienza e Intuizione",
        "file": EBOOKS_DIR / "Tra Scienza e Intuizione.docx",
        "argomenti": ["scienza", "intuizione", "fisica", "universo", "mente", "vibrazioni", "energia"],
        "pitch": "Se vuoi approfondire la vera natura della mente, trovi tutto spiegato nel mio libro 'Tra Scienza e Intuizione' su Amazon."
    }
]

def find_best_ebook(topic: str) -> dict:
    """Trova l'ebook più inerente all'argomento del video."""
    topic_lower = topic.lower()
    for ebook in EBOOKS_DB:
        for arg in ebook["argomenti"]:
            if arg in topic_lower:
                return ebook
    return EBOOKS_DB[2]  # Default a "Tra Scienza e Intuizione"

def estrai_testo_docx(file_path: Path) -> str:
    """Estrae il testo da un file .docx."""
    if not file_path.exists():
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
    
    # Prepara il prompt
    if mode == "promo":
        contenuto_libro = estrai_testo_docx(ebook["file"])
        prompt = f"""
        Sei un genio del copywriting persuasivo e sceneggiatore per un canale TikTok di divulgazione (stile documentario mozzafiato).
        Tema: {topic}. Testo da cui trarre ispirazione: {contenuto_libro[:30000]}
        
        Scrivi uno script TikTok di circa 130 parole in italiano che sia ESTREMAMENTE ORIGINALE, SENSATO E VIRALE.
        Non fare riassunti noiosi, ma estrai l'intuizione più sconvolgente o utile dal testo e raccontala come un segreto svelato.
        
        REGOLE FONDAMENTALI PER LA VOCE (MOLTO IMPORTANTE):
        - Usa frasi molto brevi, ritmate e dal forte impatto emotivo.
        - Scrivi TUTTI i numeri in lettere (es. "cento" e non "100").
        - NON USARE MAI simboli speciali, parentesi, o virgolette.
        - Inserisci punti o virgole per far prendere fiato alla voce.
        
        STRUTTURA OBBLIGATORIA:
        - RIGA 1: Scrivi "TITOLO: " seguito da un titolo magnetico che generi forte curiosità (hook irresistibile).
        - RIGA 2 in poi: 
          1. HOOK viscerale e inaspettato (es. "Quello che ti hanno detto su... è falso").
          2. VALORE sensato, originale e concreto (spiega il "perché" in modo logico e affascinante).
          3. CTA finale organica: "{ebook['pitch']} Link in bio!".
          
        Inoltre, PRIMA di rispondere con il testo, usa il tuo tool per generare 3 IMMAGINI 9:16 diverse e altamente cinematografiche per il background. DOPO averle generate, USA IL TOOL run_command per spostarle nella cartella '/home/magic/Scrivania/code/GIT/video_generator/assets/backgrounds/' con nomi come promo_bg_1.jpg, ecc.
        SE LA GENERAZIONE IMMAGINI FALLISCE PER LIMITE DI QUOTA O ERRORE 429: IGNORA il problema, NON scusarti e procedi a scrivere il copione senza menzionare l'accaduto.
        """
    elif mode == "virale":
        history_path = Path("used_news_history.txt")
        history = history_path.read_text(encoding="utf-8") if history_path.exists() else "Nessuna notizia usata finora"
        history = "\n".join(history.strip().split("\n")[-20:])
        prompt = f"""
        Sei un maestro dello storytelling virale e analista brillante per TikTok. 
        
        TEMA OBBLIGATORIO: Mente, psicologia, salute, neuroscienze, crescita personale o nutrizione.
        NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
        {history}
        
        STEP 1 (CRITICO): Usa il tool search_web per cercare su internet una notizia VERA, recentissima (ultime 24-48 ore) e altamente virale sul tema obbligatorio. Non usare conoscenze vecchie o argomenti già in lista, trova qualcosa di NUOVO.
        
        STEP 2: Scrivi un copione TikTok di circa 130 parole in italiano basato su questa notizia. Il copione deve essere SCONVOLGENTE, ORIGINALE ma profondamente SENSATO (basato sulla logica).
        Trova l'angolo più inaspettato della notizia. Perché dovrebbe importare a chi guarda? Come cambia la sua vita oggi?
        
        REGOLE FONDAMENTALI PER LA VOCE (MOLTO IMPORTANTE):
        - Usa frasi incisive, brevi, come un dialogo intimo e rivelatore.
        - Scrivi TUTTI i numeri in lettere (es. "mille" e non "1000").
        - NON USARE MAI simboli speciali, parentesi, o virgolette.
        - Inserisci punti o virgole per far prendere fiato alla voce.
        
        STRUTTURA OBBLIGATORIA:
        - RIGA 1: "TITOLO: " + un titolo shock basato su un paradosso o una forte rivelazione.
        - RIGA 2 in poi: 
          1. HOOK ipnotico nei primi 3 secondi.
          2. STORIA/SPIEGAZIONE sensata e brillante che incolla lo spettatore allo schermo.
          3. ENGAGEMENT potente: fai una domanda divisiva o profonda e chiedi la loro opinione nei commenti.
        
        CRITICO: PRIMA di scrivermi il copione, DEVI USARE il tuo strumento di generazione immagini per creare 3 IMMAGINI 9:16 fotorealistiche. Immediatamente dopo, USA IL TOOL run_command per copiarle/spostarle fisicamente in '/home/magic/Scrivania/code/GIT/video_generator/assets/backgrounds/'. Solo dopo scrivimi il copione.
        SE LA GENERAZIONE IMMAGINI FALLISCE PER LIMITE DI QUOTA O ERRORE 429: IGNORA il problema, NON scusarti e procedi a scrivere il copione senza menzionare l'accaduto.
        """
    else: # mode == "bastian"
        history_path = Path("used_news_history.txt")
        history = history_path.read_text(encoding="utf-8") if history_path.exists() else "Nessuna notizia usata finora"
        history = "\n".join(history.strip().split("\n")[-20:])
        prompt = f"""
        Sei un maestro dello storytelling virale e analista brillante per TikTok. 
        
        TEMA OBBLIGATORIO: Mente, psicologia, salute, neuroscienze, crescita personale o nutrizione.
        NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
        {history}
        
        STEP 1 (CRITICO): Usa il tool search_web per cercare su internet una notizia VERA, recentissima (ultime 24-48 ore) e dibattuta sul tema obbligatorio. Non usare conoscenze vecchie o argomenti già in lista, trova qualcosa di NUOVO.
        
        STEP 2: Scrivi un copione TikTok di circa 130 parole in italiano usando la tecnica del "Bastian Contrario" (Angolo Contrariano).
        Analizza la notizia e proponi una visione fortemente impopolare, scomoda o contro-intuitiva, ma supportata da una logica ferrea (sensato). Distruggi le credenze della massa. Perché la narrativa comune su questa notizia è sbagliata? Fai riflettere profondamente.
        
        REGOLE FONDAMENTALI PER LA VOCE (MOLTO IMPORTANTE):
        - Usa frasi incisive, brevi, come un dialogo intimo e rivelatore.
        - Scrivi TUTTI i numeri in lettere (es. "mille" e non "1000").
        - NON USARE MAI simboli speciali, parentesi, o virgolette.
        - Inserisci punti o virgole per far prendere fiato alla voce.
        
        STRUTTURA OBBLIGATORIA:
        - RIGA 1: "TITOLO: " + un titolo shock basato su un paradosso o una forte rivelazione.
        - RIGA 2 in poi: 
          1. HOOK ipnotico nei primi 3 secondi.
          2. STORIA/SPIEGAZIONE sensata e brillante che incolla lo spettatore allo schermo.
          3. ENGAGEMENT potente: fai una domanda divisiva o profonda e chiedi la loro opinione nei commenti.
        
        CRITICO: PRIMA di scrivermi il copione, DEVI USARE il tuo strumento di generazione immagini per creare 3 IMMAGINI 9:16 fotorealistiche. Immediatamente dopo, USA IL TOOL run_command per copiarle/spostarle fisicamente in '/home/magic/Scrivania/code/GIT/video_generator/assets/backgrounds/'. Solo dopo scrivimi il copione.
        SE LA GENERAZIONE IMMAGINI FALLISCE PER LIMITE DI QUOTA O ERRORE 429: IGNORA il problema, NON scusarti e procedi a scrivere il copione senza menzionare l'accaduto.
        """

    # Evoca l'agente in modo sincrono usando asyncio
    async def run_agent():
        config = LocalAgentConfig(
            system_instructions="Sei un agente sceneggiatore di TikTok e creatore di immagini. Rispondi solo con il testo finale del copione, ma prima usa SEMPRE il tool generate_image e poi il tool run_command per spostare le immagini create. Se fallisce la generazione immagini (es. 429), IGNORA l'errore e scrivi SOLO il copione senza aggiungere alcuna scusa o menzione del problema.",
            capabilities=CapabilitiesConfig()
        )
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            # Raccogliamo tutto il testo
            final_text = ""
            async for token in response:
                final_text += token
            return final_text

    try:
        script_text = asyncio.run(run_agent())
        return script_text.strip()
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
    if not api_key:
        console.print("[red]Errore:[/] Nessuna API Key per Gemini trovata.")
        console.print("Per favore imposta la variabile d'ambiente GEMINI_API_KEY oppure passa l'argomento --api-key")
        console.print("Puoi ottenere la chiave gratis da: https://aistudio.google.com/app/apikey")
        sys.exit(1)

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
