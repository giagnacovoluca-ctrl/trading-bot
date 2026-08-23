import os
import argparse
import random
import re
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

# ── 2. Nuova Lista Argomenti (Diversità Tematica, min 40 topic, categorie bilanciate) ────────────────
TOPIC_IDEAS = {
    "Neuroscienze/Mente": [
        "Neuroplasticità e apprendimento continuo",
        "Come il sonno profondo ripara i neuroni",
        "L'effetto placebo e i suoi meccanismi fisici",
        "Le basi neurali della memoria a lungo termine",
        "Come si formano le abitudini nel cervello",
        "Il legame tra microbioma intestinale e cervello",
        "Le reti neurali della consapevolezza",
        "Il cervello durante gli stati meditativi"
    ],
    "Fisica/Spazio/Astronomia": [
        "La ricerca della materia e dell'energia oscura",
        "Buchi neri e la radiazione di Hawking",
        "Rivelazione delle onde gravitazionali",
        "Esopianeti potenzialmente abitabili",
        "L'entanglement quantistico",
        "Le teorie sui viaggi nel tempo",
        "Il fondo cosmico a microonde e l'origine dell'universo",
        "Neutrini e le particelle fantasma"
    ],
    "Biologia/Evoluzione": [
        "L'epigenetica e l'impatto dell'ambiente sul DNA",
        "CRISPR e l'editing genetico",
        "L'evoluzione complessa dell'occhio umano",
        "Simbiosi e mutualismo tra specie",
        "Le grandi estinzioni di massa storiche",
        "I meccanismi cellulari dell'invecchiamento"
    ],
    "Psicologia/Comportamento": [
        "I bias cognitivi nelle decisioni quotidiane",
        "La scienza dell'intelligenza emotiva",
        "La psicologia della motivazione",
        "L'effetto Dunning-Kruger",
        "L'impatto psicologico dell'isolamento"
    ],
    "Storia/Archeologia/Misteri storici": [
        "Le scoperte nei templi di Gobekli Tepe",
        "I complessi meccanismi della Macchina di Anticitera",
        "La decifrazione della Stele di Rosetta",
        "La civiltà perduta della valle dell'Indo"
    ],
    "Tecnologia/IA/Futuro": [
        "Il funzionamento del deep learning e delle reti neurali",
        "Le interfacce dirette cervello-computer",
        "I vantaggi reali dei computer quantistici",
        "La stampa 3D di organi biologici"
    ],
    "Matematica/Paradossi logici": [
        "I teoremi di incompletezza di Gödel",
        "Il paradosso di Monty Hall e la statistica",
        "I frattali presenti in natura",
        "Le implicazioni dell'ipotesi di Riemann"
    ],
    "Filosofia/Esistenzialismo": [
        "L'attualità del mito della caverna di Platone",
        "L'eterno ritorno secondo Nietzsche",
        "L'identità e il paradosso di Teseo",
        "Il dibattito tra determinismo e libero arbitrio"
    ],
    "Economia comportamentale": [
        "La teoria del prospetto",
        "L'effetto 'nudge' e le spinte gentili",
        "L'irrazionalità nei mercati finanziari",
        "Il fenomeno dell'avversione alle perdite"
    ],
    "Ambiente/Clima/Natura": [
        "Le conseguenze dell'acidificazione degli oceani",
        "Le foreste interconnesse dalle reti di funghi micorrizici",
        "La fisica dei cambiamenti climatici",
        "Il ripristino di ecosistemi degradati"
    ],
    "Medicina/Salute basata su evidenze": [
        "La crisi della resistenza agli antibiotici",
        "I successi dell'immunoterapia oncologica",
        "La tecnologia e il potenziale dei vaccini a mRNA",
        "La scienza dietro il digiuno intermittente"
    ],
    "Sociologia/Antropologia": [
        "L'evoluzione e l'origine del linguaggio umano",
        "Le strutture familiari nelle società antiche",
        "L'effetto spettatore nelle dinamiche sociali",
        "Come i social network alterano l'antropologia moderna"
    ]
}

def pick_intelligent_topic() -> tuple[str, str]:
    """Sceglie un argomento evitando categorie già trattate di recente (ultimi 3 usati)."""
    history_path = Path("used_news_history.txt")
    recent_categories = []
    if history_path.exists():
        history = history_path.read_text(encoding="utf-8").splitlines()
        # Leggiamo le ultime 10 righe per assicurarci di trovare almeno 3 categorie
        for line in reversed(history[-10:]):
            if "CAT:" in line:
                # Esempio formato atteso: CAT: Biologia/Evoluzione | FONTE_NOTIZIA: ...
                cat_match = re.search(r'CAT:\s*(.+?)\s*\|', line)
                if cat_match:
                    recent_categories.append(cat_match.group(1).strip())
            if len(recent_categories) >= 3:
                break

    available_cats = [c for c in TOPIC_IDEAS.keys() if c not in recent_categories]
    if not available_cats:
        # Fallback se tutte le categorie sono state usate (improbabile con tante, ma safe)
        available_cats = list(TOPIC_IDEAS.keys())

    chosen_cat = random.choice(available_cats)
    chosen_topic = random.choice(TOPIC_IDEAS[chosen_cat])
    return chosen_cat, chosen_topic


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


def generate_tiktok_script(topic: str, category: str, ebook: dict, mode: str = "promo") -> str:
    """
    Usa Google Antigravity Agent per generare lo script TikTok e disegnare le immagini.
    L'agente elabora il testo in autonomia e ha i poteri per generare file grafici locali.
    """
    console.print(f"[cyan]Evocazione dell'Agente Antigravity sul tema:[/] {topic} (Categoria: {category})")
    
    history_path = Path("used_news_history.txt")
    if history_path.exists():
        raw_history = history_path.read_text(encoding="utf-8").splitlines()
        cleaned_history = []
        for line in raw_history:
            if "SCOPERTA ASSURDA" in line or "Errore di Generazione" in line or "SKIP" in line:
                continue
            cleaned_history.append(line)
        history_text = "\n".join(cleaned_history[-20:]) if cleaned_history else "Nessuna notizia valida usata finora"
    else:
        history_text = "Nessuna notizia usata finora"
    
    # REGOLE COMUNI ANTI-DISINFORMAZIONE
    anti_misinfo_rules = """
        REGOLE ANTI-DISINFORMAZIONE E VERIDICITÀ (CRITICHE E IMPERATIVE):
        - VIETATI TITOLI E FRASI COME: "SCOPERTA ASSURDA", "SEGRETO NASCOSTO", "TI STANNO MENTENDO", "SHOCK".
        - VIETATO esagerare, distorcere o speculare sui risultati scientifici (es. non dire mai "il sangue hackerava il cervello").
        - OBBLIGATORIO: Citare SEMPRE la fonte reale (es. un'università, il nome del journal come Nature o Science) in modo chiaro.
        - OBBLIGATORIO: Usa un linguaggio preciso. Usa "I ricercatori suggeriscono" o "Uno studio ha osservato" INVECE di "È provato che" o "È scientificamente provato".
        - OBBLIGATORIO: Non trasformare MAI una correlazione in una causalità certa.
        - OBBLIGATORIO: Aggiungere sempre frasi come "secondo uno studio di [fonte]" o simili quando si citano dati.
        
        ANTI-SENSAZIONALISMO nel TITOLO: Il titolo DEVE essere una frase affascinante ma accurata (max 5 parole), NON un titolo click-bait falso. 
        Esempi BUONI: 'Il cervello ripara se stesso', 'La luce solare crea entanglement'. 
        Esempi VIETATI: 'SHOCK: Il tuo sangue ti hackerava!', 'SEGRETO che Big Pharma nasconde'
    """

    # Prepara il prompt
    if mode == "promo":
        contenuto_libro = estrai_testo_docx(ebook["file"])
        prompt = f"""
        Sei un genio del copywriting persuasivo e sceneggiatore per un canale TikTok di divulgazione (stile documentario accurato).
        Tema: {topic}. Testo da cui trarre ispirazione: {contenuto_libro[:30000]}
        
        CONCETTI GIA' TRATTATI RECENTEMENTE (EVITA ASSOLUTAMENTE DI RIPETERLI. Trova un angolo, capitolo o concetto NUOVO e DIVERSO):
        {history_text}
        
        {anti_misinfo_rules}
        
        Scrivi uno script TikTok di circa 130 parole in italiano che sia ESTREMAMENTE ORIGINALE, SENSATO E INTERESSANTE.
        Non fare riassunti noiosi, ma estrai un'intuizione utile dal testo. Scegli casualmente un dettaglio diverso per evitare di essere ripetitivo.
        
        REGOLE FONDAMENTALI PER LA VOCE (MOLTO IMPORTANTE):
        - Usa frasi molto brevi, ritmate e dal forte impatto emotivo.
        - Scrivi TUTTI i numeri in lettere (es. "cento" e non "100").
        - NON USARE MAI simboli speciali, parentesi, o virgolette.
        - Inserisci punti o virgole per far prendere fiato alla voce.
        
        STRUTTURA OBBLIGATORIA (DEVI RISPETTARE ESATTAMENTE QUESTO FORMATO, NESSUNA PAROLA IN PIU'):
        TITOLO: <il tuo titolo accurato qui (max 5 parole)>
        TESTO: <il tuo copione qui (circa 130 parole), inclusa la CTA>
        FONTE_NOTIZIA: <fonte reale o libro da cui hai tratto l'informazione (max 1 riga)>
          
        CRITICO: PRIMA di scrivermi il copione, DEVI USARE il tuo strumento di generazione immagini per creare 7 IMMAGINI 9:16. Le immagini DEVONO RAPPRESENTARE ESATTAMENTE e in modo fedele ciò di cui parla la voce nel copione (scene, oggetti, personaggi o concetti citati). NON devono essere immagini a caso sul tema generale. Assicurati inoltre che abbiano uno stile visivo vario e adatto alla scena. Immediatamente dopo, USA IL TOOL run_command per copiarle/spostarle fisicamente in '/home/ubuntu/GIT/video_generator/assets/backgrounds/'.
        SOLO DOPO aver completato le immagini, rispondi scrivendo il copione ESATTAMENTE con il formato richiesto (TITOLO:, TESTO:, FONTE_NOTIZIA:) e nient'altro.
        SE LA GENERAZIONE IMMAGINI FALLISCE: procedi comunque scrivendo il copione.
        """
    elif mode == "virale":
        prompt = f"""
        Sei un maestro dello storytelling e analista brillante per TikTok. 
        
        TEMA ASSEGNATO: {topic} (Categoria: {category}). Sii preciso.
        NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
        {history_text}
        
        MODALITA' VIRALE MIGLIORATA:
        STEP 1: Usa il tool search_web per cercare una notizia VERA, recente e verificabile (degli ultimi 7 giorni) sul tema. Non usare l'orizzonte "ultime 24h" se genera allucinazioni. Cerca eventi reali.
        STEP 2: Verifica che la fonte sia credibile (es. Nature, Science, università accreditate, giornali scientifici. NO blog o tabloid).
        STEP 3: Se la notizia NON è verificabile, scegli UN'ALTRA NOTIZIA fino a trovarne una autentica e solida.
        
        {anti_misinfo_rules}
        
        STEP 4: Scrivi un copione TikTok di circa 130 parole in italiano basato su questa notizia. Il copione deve essere ACCATTIVANTE ma profondamente ACCURATO (basato sulla logica e sulle vere scoperte).
        Trova l'angolo più inaspettato della notizia spiegando perché è rilevante.
        
        REGOLE FONDAMENTALI PER LA VOCE E IL TESTO:
        - Usa frasi incisive, brevi, come un dialogo intimo e rivelatore.
        - Scrivi TUTTI i numeri in lettere (es. "mille" e non "1000").
        - NON USARE MAI simboli speciali, parentesi, o virgolette.
        - Inserisci punti o virgole per far prendere fiato alla voce.
        
        STRUTTURA OBBLIGATORIA (DEVI RISPETTARE ESATTAMENTE QUESTO FORMATO, NESSUNA PAROLA IN PIU'):
        TITOLO: <il tuo titolo accurato qui (max 5 parole)>
        TESTO: <il tuo copione qui (circa 130 parole), con le regole vocali applicate>
        FONTE_NOTIZIA: <nome della fonte verificata, es. 'Studio dell'Università di Harvard' oppure 'Articolo su Nature' (max 1 riga)>
        
        CRITICO: PRIMA di scrivermi il copione, DEVI USARE il tuo strumento di generazione immagini per creare 7 IMMAGINI 9:16. Le immagini DEVONO RAPPRESENTARE ESATTAMENTE e in modo fedele ciò di cui parla la voce nel copione. NON devono essere immagini a caso sul tema generale. Assicurati inoltre che abbiano uno stile visivo vario e adatto alla scena. Immediatamente dopo, USA IL TOOL run_command per copiarle/spostarle fisicamente in '/home/ubuntu/GIT/video_generator/assets/backgrounds/'.
        SOLO DOPO aver completato le immagini, rispondi scrivendo il copione ESATTAMENTE con il formato richiesto (TITOLO:, TESTO:, FONTE_NOTIZIA:) e nient'altro.
        SE LA GENERAZIONE IMMAGINI FALLISCE: procedi comunque scrivendo il copione.
        """
    else: # mode == "bastian"
        prompt = f"""
        Sei un maestro dello storytelling e analista brillante per TikTok. 
        
        TEMA ASSEGNATO: {topic} (Categoria: {category}). Sii preciso.
        NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
        {history_text}
        
        MODALITA' BASTIAN CONTRARIO:
        STEP 1: Usa il tool search_web per cercare una notizia VERA, recente e dibattuta (degli ultimi 7 giorni) sul tema.
        STEP 2: Verifica che la fonte sia credibile.
        STEP 3: Se la notizia NON è verificabile, cercane un'altra.
        
        {anti_misinfo_rules}
        
        STEP 4: Scrivi un copione TikTok di circa 130 parole in italiano usando la tecnica del "Bastian Contrario" (Angolo Contrariano).
        Analizza la notizia e proponi una visione impopolare, scomoda o contro-intuitiva, ma supportata da una logica ferrea e dati precisi.
        
        REGOLE FONDAMENTALI PER LA VOCE E IL TESTO:
        - Usa frasi incisive, brevi.
        - Scrivi TUTTI i numeri in lettere.
        - NON USARE MAI simboli speciali, parentesi, o virgolette.
        - Inserisci punti o virgole per far prendere fiato.
        
        STRUTTURA OBBLIGATORIA (DEVI RISPETTARE ESATTAMENTE QUESTO FORMATO, NESSUNA PAROLA IN PIU'):
        TITOLO: <il tuo titolo accurato qui (max 5 parole)>
        TESTO: <il tuo copione qui (circa 130 parole)>
        FONTE_NOTIZIA: <nome della fonte verificata della notizia reale (max 1 riga)>
        
        CRITICO: PRIMA di scrivermi il copione, DEVI USARE il tuo strumento di generazione immagini per creare 7 IMMAGINI 9:16. Le immagini DEVONO RAPPRESENTARE ESATTAMENTE e in modo fedele ciò di cui parla la voce nel copione. Immediatamente dopo, USA IL TOOL run_command per copiarle/spostarle fisicamente in '/home/ubuntu/GIT/video_generator/assets/backgrounds/'.
        SOLO DOPO aver completato le immagini, rispondi scrivendo il copione ESATTAMENTE con il formato richiesto (TITOLO:, TESTO:, FONTE_NOTIZIA:) e nient'altro.
        SE LA GENERAZIONE IMMAGINI FALLISCE: procedi comunque scrivendo il copione.
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
        return "TITOLO: Errore di Generazione\nTESTO: Siamo spiacenti, c'è stato un problema.\nFONTE_NOTIZIA: Nessuna"

def save_history(category: str, script_text: str):
    """Salvataggio intelligente nella history: salva solo la FONTE_NOTIZIA reale."""
    # Cerchiamo la riga FONTE_NOTIZIA:
    fonte_match = re.search(r'FONTE_NOTIZIA:\s*(.*)', script_text, re.IGNORECASE)
    
    if fonte_match:
        fonte = fonte_match.group(1).strip()
        if not fonte or "errore di generazione" in fonte.lower():
            fonte = "SKIP"
    else:
        fonte = "SKIP"
        
    with open("used_news_history.txt", "a", encoding="utf-8") as f:
        f.write(f"CAT: {category} | FONTE_NOTIZIA: {fonte}\n")

def main():
    parser = argparse.ArgumentParser(description="RAG Script Generator per TikTok (Gemini API)")
    parser.add_argument("--topic", default="auto", help="Argomento specifico, oppure 'auto' per scegliere automaticamente")
    parser.add_argument("--output", default="scripts/script_generato.txt", help="Dove salvare lo script")
    parser.add_argument("--mode", default="promo", choices=["promo", "virale", "bastian"], help="Modalità di generazione")
    args = parser.parse_args()

    if args.topic.lower() == "auto":
        category, topic = pick_intelligent_topic()
        console.print(f"[green]Topic scelto automaticamente: {topic} (Categoria: {category})[/]")
    else:
        topic = args.topic
        category = "Manuale"

    # 1. Recupero (Retrieval) dell'ebook giusto
    ebook = find_best_ebook(topic)
    
    # 2. Generazione (Augmented Generation)
    script_text = generate_tiktok_script(topic, category, ebook, args.mode)
    
    # 3. Salvataggio della history intelligente
    save_history(category, script_text)
    
    # 4. Salvataggio su file del copione
    out_path = Path(args.output)
    out_path.parent.mkdir(exist_ok=True, parents=True)
    
    ebook_filename = ebook["file"].name if ebook["file"] else ""
    final_text = script_text + f"\nEBOOK_FILE: {ebook_filename}"
    
    out_path.write_text(final_text, encoding="utf-8")
    
    console.print(f"[bold green]✓ Script salvato in {out_path}[/]")
    console.print(f"Anteprima:\n[dim]{script_text}[/dim]")

if __name__ == "__main__":
    main()
