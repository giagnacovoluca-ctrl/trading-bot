import os
import argparse
import datetime
import json
import random
import re
from pathlib import Path
from rich.console import Console
import docx2txt
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
    },
    {
        "titolo": "Il potere curativo dell'Acqua",
        "file": EBOOKS_DIR / "Libro_Completo_Formato_Amazon.docx",
        "argomenti": ["acqua", "idratazione", "benessere", "salute", "energia", "purezza", "depurazione"],
        "pitch": "Scopri i segreti dell'idratazione profonda e della purificazione cellulare leggendo il mio nuovo manuale sull'Acqua."
    },
    {
        "titolo": "Epigenetica: Riprogramma il tuo DNA",
        "file": EBOOKS_DIR / "Epigenetica_Libro_Completo_Amazon.docx",
        "argomenti": ["epigenetica", "dna", "genetica", "biologia", "salute", "evoluzione", "longevità", "riprogrammazione"],
        "pitch": "Scopri come il tuo stile di vita può modificare l'espressione dei tuoi geni leggendo il mio nuovo manuale sull'Epigenetica."
    },
    {
        "titolo": "Attiva il Nervo Vago",
        "file": EBOOKS_DIR / "Nervo_Vago_Libro_Completo_Amazon.docx",
        "argomenti": ["nervo vago", "stress", "calma", "sistema nervoso", "rilassamento", "respiro", "psicologia", "ansia"],
        "pitch": "Impara le tecniche per attivare il nervo vago e sconfiggere ansia e stress leggendo il mio nuovo manuale dedicato."
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
        "Il ripristino di ecosistemi degradati",
        "Il potere curativo dell'acqua e l'idratazione cellulare profonda",
        "I segreti dell'acqua e la memoria dei fluidi in natura",
        "L'importanza dell'acqua per il mantenimento dell'energia vitale"
    ],
    "Medicina/Salute basata su evidenze": [
        "La crisi della resistenza agli antibiotici",
        "I successi dell'immunoterapia oncologica",
        "La tecnologia e il potenziale dei vaccini a mRNA",
        "La scienza dietro il digiuno intermittente",
        "L'epigenetica e come lo stile di vita riprogramma il DNA",
        "Come stimolare il nervo vago per ridurre ansia e stress",
        "L'attivazione del nervo vago e i benefici sul sistema nervoso"
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

    # REGOLE COMUNI ANTI-DISINFORMAZIONE E SICUREZZA TIKTOK
    anti_misinfo_rules = """
        REGOLE ANTI-DISINFORMAZIONE E VERIDICITÀ (CRITICHE E IMPERATIVE):
        - VIETATI ASSOLUTAMENTE TITOLI E FRASI COME: "SCOPERTA ASSURDA", "SEGRETO NASCOSTO", "TI STANNO MENTENDO", "SHOCK".
        - VIETATO l'uso di linguaggio allarmistico legato alla biologia o alla medicina. NON usare MAI parole come "invasione", "mutazione", "parassita", "distruggere", "hackerare" riferite al corpo, al cervello o alla salute.
        - TikTok banna i video che generano "procurato allarme" o "disinformazione medica". Usa un tono EDUCATIVO, CALMO E AFFASCINANTE. Non spaventare l'utente.
        - OBBLIGATORIO: Citare SEMPRE la fonte reale in modo chiaro.
        - OBBLIGATORIO: Usa un linguaggio preciso come "I ricercatori suggeriscono" o "Uno studio ha osservato".
        - OBBLIGATORIO: Non trasformare MAI una correlazione in una causalità certa.

        ANTI-SENSAZIONALISMO nel TITOLO: Il titolo DEVE essere una frase affascinante ma accurata (max 5 parole), NON un titolo click-bait spaventoso.
        Esempi BUONI: 'Come il cervello si ripara', 'La luce solare e l'energia'.
        Esempi VIETATI: 'Invasione segreta nel tuo cervello', 'Stai mutando senza saperlo', 'Il tuo corpo sta marcendo'.
    """

    # Prepara il prompt
    if mode == "promo":
        contenuto_libro = estrai_testo_docx(ebook["file"])
        chunk_size = 80000
        if len(contenuto_libro) > chunk_size:
            import random
            start = random.randint(0, len(contenuto_libro) - chunk_size)
            # Find next period to avoid mid-sentence cuts
            next_period = contenuto_libro.find('.', start)
            if next_period != -1 and next_period < start + 1000:
                start = next_period + 1
            chunk = contenuto_libro[start:start+chunk_size]
        else:
            chunk = contenuto_libro

        prompt = f"""
        Sei uno storyteller e creator virale di TikTok specializzato in divulgazione affascinante.
        Tema: {topic}. Testo da cui trarre ispirazione (ESTRATTO DELL'EBOOK): {chunk}

        CONCETTI GIA' TRATTATI RECENTEMENTE (EVITA DI RIPETERLI):
        {history_text}

        {anti_misinfo_rules}

        REGOLE ANTI-ALLUCINAZIONE (FONDAMENTALI):
        - DEVI basarti STRETTAMENTE sull'estratto dell'ebook fornito. Non inventare cure o terapie.

        Scrivi uno script TikTok di circa 130 parole in italiano.
        DEVE SUONARE NATURALE, UMANO E COLLOQUIALE. Elimina il tono robotico o troppo impostato. Parla come se stessi svelando un segreto affascinante a un amico.

        REGOLE FONDAMENTALI PER LA VOCE:
        - Usa frasi brevi, ritmate e dal forte impatto emotivo.
        - Scrivi TUTTI i numeri in lettere (es. "cento" e non "100").
        - NON USARE MAI simboli speciali, parentesi, o virgolette. Inserisci punti o virgole per far prendere fiato.

        STRUTTURA DEL COPIONE:
        TITOLO: <il tuo titolo accurato qui (max 5 parole)>
        TESTO:
        ATTO 1: Un hook diretto, curioso e conversazionale. Niente formule noiose. Arriva subito al punto (max 20 parole).
        ATTO 2: Spiega il concetto principale estratto dal libro in modo estremamente semplice e visivo.
        ATTO 3: Svela una conseguenza sorprendente o un colpo di scena che riguarda la vita di chi guarda.
        ATTO 4: CTA finale breve (max 10 parole).
        FONTE_NOTIZIA: <nome dell'ebook (max 1 riga)>

        ATTENZIONE CRITICA: LA TUA RISPOSTA DEVE INIZIARE DIRETTAMENTE CON LA PAROLA "TITOLO:".
        NON INSERIRE NESSUN COMMENTO, NESSUNA INTRODUZIONE, NIENTE.
        """
    elif mode == "virale":
        prompt = f"""
        Sei un maestro dello storytelling e analista brillante per TikTok.

        TEMA ASSEGNATO: {topic} (Categoria: {category}). Sii preciso.
        NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
        {history_text}

        MODALITA' VIRALE MIGLIORATA:
        STEP 1: Scegli una notizia VERA, recente e verificabile sul tema, tratta dal tuo database interno. Cerca eventi reali.
        STEP 2: Verifica mentalmente che la fonte sia credibile (es. Nature, Science, università accreditate, giornali scientifici. NO blog o tabloid).
        STEP 3: Scegli una notizia verificabile e concreta.

        {anti_misinfo_rules}

        REGOLE ANTI-ALLUCINAZIONE AGGIUNTIVE (CRITICHE):
        - VIETATO combinare istituzioni o studi da domini DIVERSI in una singola fonte fittizia (es. non dire "studio NASA sulla dieta" o "ricerca Harvard-ESA sull'intestino"). Ogni istituzione fa ricerca nel suo dominio.
        - VIETATO inventare nomi di riviste, università, o studi. Se non sei certo della fonte, scrivi "ricercatori dell'Università di [paese reale]" o "uno studio su [journal reale]".
        - REGOLA FONDAMENTALE: Se dopo 3 ricerche non trovi una notizia recente verificabile, usa invece un FATTO SCIENTIFICO BEN STABILITO e documentato (non deve essere "recente", deve essere VERO). In questo caso FONTE_NOTIZIA = "Letteratura scientifica consolidata su [argomento]".

        STEP 4: Scrivi un copione TikTok di circa 130 parole in italiano basato su questa notizia. Il copione deve essere ACCATTIVANTE ma profondamente ACCURATO.

        REGOLE FONDAMENTALI PER LA VOCE E IL TESTO:
        - Usa frasi incisive, brevi, come un dialogo intimo e rivelatore.
        - Scrivi TUTTI i numeri in lettere (es. "mille" e non "1000").
        - NON USARE MAI simboli speciali, parentesi, o virgolette.
        - Inserisci punti o virgole per far prendere fiato alla voce.
        - OGNI ATTO deve rispettare il limite di parole indicato (non superarlo).

        STRUTTURA OBBLIGATORIA DEL COPIONE (5 ATTI — RISPETTA QUESTA STRUTTURA ESATTA):
        TITOLO: <il tuo titolo accurato qui (max 5 parole)>
        TESTO:
        ATTO 1 — HOOK (15-20 parole MAX): Un fatto contro-intuitivo che rompe le aspettative in modo calmo e curioso. NON iniziare mai con le solite formule ripetitive (es. vieta assoluto di usare frasi come "Quello che pensi", "Sapevi che", "Ogni volta che"). Inizia immergendo subito lo spettatore nell'argomento con una frase diretta, poetica o curiosa. Ogni video deve avere un incipit stilisticamente diverso.
        ATTO 2 — CONTESTO (25-30 parole MAX): Perché questo è rilevante per la vita quotidiana. Connetti il dato scientifico all'esperienza personale.
        ATTO 3 — RIVELAZIONE (40-50 parole MAX): Il dato/studio con fonte verificata e COERENTE col dominio della ricerca. Usa 'Secondo uno studio dell'[istituzione reale nel settore]...' — NON trasformare correlazioni in causalità.
        ATTO 4 — COLPO DI SCENA (15-20 parole MAX): L'implicazione inaspettata che nessuno aveva considerato.
        ATTO 5 — CTA (10-15 parole MAX): Azione specifica che l'utente può fare ORA. Non 'seguimi'.
        FONTE_NOTIZIA: <nome della fonte verificata. Se hai usato un fatto consolidato scrivi "Letteratura scientifica su [argomento]">

        ATTENZIONE CRITICA: LA TUA RISPOSTA DEVE INIZIARE DIRETTAMENTE CON LA PAROLA "TITOLO:".
        NON INSERIRE NESSUN COMMENTO, NESSUNA INTRODUZIONE, NIENTE. RISPONDI SOLO CON IL COPIONE NEL FORMATO ESATTO RICHIESTO.
        """
    else: # mode == "bastian"
        prompt = f"""
        Sei un maestro dello storytelling e analista brillante per TikTok.

        TEMA ASSEGNATO: {topic} (Categoria: {category}). Sii preciso.
        NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
        {history_text}

        MODALITA' BASTIAN CONTRARIO:
        STEP 1: Scegli una notizia VERA, recente e dibattuta sul tema, tratta dal tuo database interno.
        STEP 2: Assicurati che la fonte sia credibile e che la notizia sia verificabile.

        {anti_misinfo_rules}

        STEP 4: Scrivi un copione TikTok di circa 130 parole in italiano usando la tecnica del "Bastian Contrario" (Angolo Contrariano).
        Analizza la notizia e proponi una visione impopolare, scomoda o contro-intuitiva, ma supportata da una logica ferrea e dati precisi.

        REGOLE FONDAMENTALI PER LA VOCE E IL TESTO:
        - Usa frasi incisive, brevi.
        - Scrivi TUTTI i numeri in lettere.
        - NON USARE MAI simboli speciali, parentesi, o virgolette.
        - Inserisci punti o virgole per far prendere fiato.

        STRUTTURA OBBLIGATORIA DEL COPIONE (5 ATTI — RISPETTA QUESTA STRUTTURA ESATTA):
        TITOLO: <il tuo titolo accurato qui (max 5 parole)>
        TESTO:
        ATTO 1 — HOOK (15-20 parole MAX): Un fatto contro-intuitivo o domanda destabilizzante che rompe le aspettative. NON iniziare mai con formule prefabbricate. Niente "Sapevi che", niente "Quello che pensi". Vai dritto al punto con una dichiarazione forte o un ragionamento controcorrente, usando ogni volta una struttura logica nuova.
        ATTO 2 — CONTESTO (25-30 parole): Perché questo è rilevante per la vita quotidiana di chi guarda. Connetti il dato scientifico all'esperienza personale.
        ATTO 3 — RIVELAZIONE (40-50 parole): Il dato/studio con fonte verificata. Usa 'Secondo uno studio dell'[università/journal]...' — NON trasformare correlazioni in causalità.
        ATTO 4 — COLPO DI SCENA (15-20 parole): L'implicazione inaspettata che nessuno aveva considerato. La parte che fa venire voglia di condividere.
        ATTO 5 — CTA (10-15 parole): Azione specifica che l'utente può fare ORA. Non 'seguimi', ma qualcosa di concreto.
        FONTE_NOTIZIA: <nome della fonte verificata della notizia reale (max 1 riga)>

        ATTENZIONE CRITICA: LA TUA RISPOSTA DEVE INIZIARE DIRETTAMENTE CON LA PAROLA "TITOLO:".
        NON INSERIRE NESSUN COMMENTO, NESSUNA INTRODUZIONE, NIENTE. RISPONDI SOLO CON IL COPIONE NEL FORMATO ESATTO RICHIESTO.
        """

    console.print(f"[dim]Generazione del copione in corso tramite AGY CLI...[/]")

    try:
        import subprocess

        result = subprocess.run(
            ["agy", "--print", prompt],
            capture_output=True,
            text=True,
            check=True
        )

        script = result.stdout.strip()
        return script
    except Exception as e:
        console.print(f"[red]Errore Antigravity:[/] {e}")
        return "TITOLO: Errore di Generazione\nTESTO: Siamo spiacenti, c'è stato un problema.\nFONTE_NOTIZIA: Nessuna"


def ab_test_hook(topic: str, script_text: str) -> str:
    """
    A/B Test Hook: genera una variante alternativa dell'ATTO 1 e sceglie
    la più forte tra le due tramite il quality validator (AGY flash).
    Ritorna il copione con l'hook migliore sostituito.
    Tempo extra stimato: ~15s.
    """
    import subprocess, re

    # Estrai l'hook corrente dal copione
    hook_match = re.search(r'ATTO 1[^\n]*\n(.*?)(?=ATTO 2|\Z)', script_text, re.DOTALL | re.IGNORECASE)
    if not hook_match:
        console.print("[dim yellow]A/B hook: impossibile estrarre ATTO 1, skip.[/]")
        return script_text

    hook_a = hook_match.group(1).strip()

    # Genera una variante B con angolo diverso
    ab_prompt = f"""Hai questo ATTO 1 (hook) per un video TikTok scientifico sul tema '{topic}':
HOOK A: {hook_a}

Scrivi UNA SOLA frase alternativa di 15-20 parole massimo per l'ATTO 1, con un angolo completamente diverso.
NON iniziare MAI con 'Sapevi che', 'Quello che pensi', o formule noiose. Usa un tono narrativo, affascinante e diretto, calando subito lo spettatore nell'azione o nel paradosso.
Rispondi SOLO con la frase hook, nient'altro."""

    try:
        import subprocess

        result_b = subprocess.run(
            ["agy", "--print", ab_prompt],
            capture_output=True,
            text=True,
            check=True
        )
        hook_b = result_b.stdout.strip().replace('"', '').replace('HOOK B:', '').strip()

        if not hook_b or len(hook_b) < 10:
            return script_text

        console.print(f"[cyan]A/B Hook Test[/]\n  A: {hook_a[:80]}...\n  B: {hook_b[:80]}...")

        judge_prompt = f"""Sei un esperto di TikTok. Scegli il hook più efficace per trattenere l'attenzione nei primi 3 secondi:
HOOK A: {hook_a}
HOOK B: {hook_b}
Rispondi SOLO con 'A' o 'B' e nient'altro."""

        result_judge = subprocess.run(
            ["agy", "--print", judge_prompt],
            capture_output=True,
            text=True,
            check=True
        )
        winner = result_judge.stdout.strip().upper()

        if "B" in winner and "A" not in winner:
            console.print(f"[green]✓ Hook B vince! Sostituisco nel copione.[/]")
            script_text = script_text.replace(hook_a, hook_b, 1)
        else:
            console.print(f"[green]✓ Hook A confermato (o parità).[/]")

    except Exception as e:
        console.print(f"[dim yellow]A/B hook test fallito ({e}), uso hook originale.[/]")

    return script_text

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

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    entry = {"timestamp": timestamp, "category": category, "fonte_notizia": fonte}
    with open("used_news_history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # Compatibilità con i prompt e gli script cron esistenti.
    with open("used_news_history.txt", "a", encoding="utf-8") as f:
        f.write(f"CAT: {category} | FONTE_NOTIZIA: {fonte}\n")

def main():
    parser = argparse.ArgumentParser(description="RAG Script Generator per TikTok (Gemini API)")
    parser.add_argument("--topic", default="auto", help="Argomento specifico, oppure 'auto' per scegliere automaticamente")
    parser.add_argument("--category", default="Manuale", help="Categoria del topic (se nota)")
    parser.add_argument("--output", default="scripts/script_generato.txt", help="Dove salvare lo script")
    parser.add_argument("--mode", default="promo", choices=["promo", "virale", "bastian"], help="Modalità video")
    parser.add_argument("--force-new", action="store_true", help="Forza rigenerazione")
    args = parser.parse_args()

    if args.topic.lower() == "auto":
        category, topic = pick_intelligent_topic()
        console.print(f"[green]Topic scelto automaticamente: {topic} (Categoria: {category})[/]")
    else:
        topic = args.topic
        category = args.category

    # 1. Recupero (Retrieval) dell'ebook giusto
    ebook = find_best_ebook(topic)

    # 2. Generazione (Augmented Generation)
    script_text = generate_tiktok_script(topic, category, ebook, args.mode)

    # 2b. A/B Hook Test: testa e sostituisce l'hook se ne esiste uno più forte (~15s extra)
    if "Errore di Generazione" not in script_text:
        console.print("[cyan]⚡ A/B Hook Test in corso...[/]")
        script_text = ab_test_hook(topic, script_text)

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
