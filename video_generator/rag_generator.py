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
from modules.script_quality import extract_metadata
from modules.ebook_catalog import ebook_to_rag, get_ebook, load_ebook_catalog

load_dotenv()

console = Console()

# ── 1. Database degli eBook Reali (su disco esterno) ────────────────
EBOOKS_DIR = Path("/home/ubuntu/ebooks")

EBOOKS_DB = [ebook_to_rag(book) for book in load_ebook_catalog()]

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
        "Come la disidratazione influenza attenzione e prestazioni cognitive",
        "Il ruolo degli oceani nell'assorbimento del calore globale",
        "Come le zone umide proteggono dalle alluvioni"
    ],
    "Medicina/Salute basata su evidenze": [
        "La crisi della resistenza agli antibiotici",
        "I successi dell'immunoterapia oncologica",
        "La tecnologia e il potenziale dei vaccini a mRNA",
        "La scienza dietro il digiuno intermittente",
        "Cosa sappiamo davvero del nervo vago e della risposta allo stress",
        "Perché il sonno influenza il sistema immunitario",
        "Come l'esercizio fisico modifica il metabolismo"
    ],
    "Sociologia/Antropologia": [
        "L'evoluzione e l'origine del linguaggio umano",
        "Le strutture familiari nelle società antiche",
        "L'effetto spettatore nelle dinamiche sociali",
        "Come i social network alterano l'antropologia moderna"
    ],
    "Alimentazione/Combinazioni di cibi": [
        "Vitamina C e ferro vegetale nello stesso pasto",
        "Legumi e cereali nella tradizione mediterranea",
        "Curcuma e pepe nero tra cucina ed evidenze",
        "Grassi alimentari e assorbimento delle vitamine liposolubili",
        "Fermentati e fibre in un'alimentazione varia",
        "Pomodoro cotto e biodisponibilità del licopene",
        "Tè e assorbimento del ferro durante i pasti",
        "Come costruire un pasto saziante con proteine fibre e grassi"
    ],
    "Superfood/Nutrizione pratica": [
        "Che cosa significa davvero superfood",
        "Frutti di bosco e antociani senza promesse miracolose",
        "Cacao amaro tra nutrienti e quantità reali",
        "Semi di lino e fonti vegetali di omega tre",
        "Legumi economici rispetto ai superfood di moda",
        "Spezie tradizionali tra gusto e ricerca nutrizionale",
        "Verdure crucifere nella cucina quotidiana",
        "Come leggere le promesse nutrizionali sui social"
    ],
    "Biografie/Abitudini di grandi personaggi": [
        "Il metodo di lavoro di Leonardo da Vinci nei suoi taccuini",
        "La routine creativa documentata di Maya Angelou",
        "Come Benjamin Franklin organizzava le sue giornate",
        "Le passeggiate nel metodo di lavoro di Charles Darwin",
        "La disciplina di Marie Curie tra laboratorio e studio",
        "Il sistema di appunti e lettura di Umberto Eco",
        "Le abitudini di scrittura documentate di Stephen King",
        "Come Nelson Mandela allenò pazienza e autocontrollo"
    ],
    "Grandi imperi/Storia sociale": [
        "Come le strade sostenevano l'Impero romano",
        "Il sistema postale dell'Impero persiano achemenide",
        "Commercio e amministrazione nell'Impero del Mali",
        "Le reti di messaggeri dell'Impero inca",
        "La gestione multiculturale dell'Impero mongolo",
        "Acqua e organizzazione urbana nell'Impero Khmer",
        "Biblioteche e traduzioni nell'età abbaside",
        "Perché alcuni grandi imperi persero coesione"
    ],
    "Rituali/Popoli antichi": [
        "Il simposio nella società dell'antica Grecia",
        "Riti di passaggio nella Roma antica",
        "Il significato del tè nelle tradizioni dell'Asia orientale",
        "Calendari agricoli e rituali stagionali antichi",
        "Riti funerari egizi e idea della memoria",
        "Il ruolo sociale dei racconti orali nelle culture antiche",
        "Feste del raccolto nel Mediterraneo antico",
        "Purificazione e ospitalità nelle società del passato"
    ],
    "Culture vive/Civiltà contemporanee": [
        "La gestione comunitaria dell'acqua nelle Ande",
        "Tradizioni di longevità a Okinawa senza mitizzazioni",
        "La cultura Sámi tra allevamento e modernità",
        "Conoscenze ecologiche dei popoli aborigeni australiani",
        "Le comunità matrilineari Minangkabau in Indonesia",
        "La tradizione Māori del legame con il territorio",
        "Nomadismo pastorale e adattamento in Mongolia",
        "Come le lingue minoritarie mantengono viva una cultura"
    ],
    "Esoterismo/Storia delle idee": [
        "L'alchimia come antenata simbolica della chimica",
        "Origine storica dei tarocchi prima dell'uso divinatorio",
        "Ermetismo nel Rinascimento europeo",
        "Astrologia e potere nelle corti antiche",
        "Il simbolismo dei labirinti nelle diverse culture",
        "Numerologia nella storia delle religioni",
        "Oracoli antichi tra rituale e decisione politica",
        "Come distinguere simbolismo spirituale e prova scientifica"
    ]
}


CATEGORY_EDITORIAL_GUIDANCE = {
    "Alimentazione/Combinazioni di cibi": "Offri un contenuto pratico basato su fonti nutrizionali affidabili. Indica quantità e limiti quando rilevanti; niente cure o superpoteri alimentari.",
    "Superfood/Nutrizione pratica": "Smonta il marketing senza demonizzare gli alimenti. Parla di nutrienti, contesto della dieta e porzioni; niente promesse miracolose.",
    "Biografie/Abitudini di grandi personaggi": "Racconta una sola abitudine documentata da biografie, lettere, interviste o archivi. Non affermare che quell'abitudine abbia causato il successo e non inventare routine.",
    "Grandi imperi/Storia sociale": "Racconta un meccanismo concreto di governo, commercio, logistica o vita quotidiana usando una fonte storica o museale autorevole. Non presentarlo come scoperta recente se non lo è.",
    "Rituali/Popoli antichi": "Descrivi significato, contesto e funzione sociale del rituale con rispetto. Evita misteri inventati, sensazionalismo e generalizzazioni sui popoli.",
    "Culture vive/Civiltà contemporanee": "Parla di comunità tuttora esistenti al presente, senza definirle primitive o congelate nel passato. Usa fonti antropologiche o istituzionali e riconosci la varietà interna.",
    "Esoterismo/Storia delle idee": "Tratta credenze, simboli e pratiche come storia culturale. Distingui esplicitamente tradizione e interpretazione da effetti scientificamente dimostrati.",
}

EDITORIAL_FAMILIES = {
    "Neuroscienze/Mente": "Mente e comportamento",
    "Psicologia/Comportamento": "Mente e comportamento",
    "Economia comportamentale": "Mente e comportamento",
    "Filosofia/Esistenzialismo": "Mente e comportamento",
    "Alimentazione/Combinazioni di cibi": "Alimentazione",
    "Superfood/Nutrizione pratica": "Alimentazione",
    "Medicina/Salute basata su evidenze": "Alimentazione",
    "Biografie/Abitudini di grandi personaggi": "Persone e abitudini",
    "Storia/Archeologia/Misteri storici": "Storia culture e simboli",
    "Grandi imperi/Storia sociale": "Storia culture e simboli",
    "Rituali/Popoli antichi": "Storia culture e simboli",
    "Culture vive/Civiltà contemporanee": "Storia culture e simboli",
    "Esoterismo/Storia delle idee": "Storia culture e simboli",
    "Sociologia/Antropologia": "Storia culture e simboli",
    "Fisica/Spazio/Astronomia": "Scienza e natura",
    "Biologia/Evoluzione": "Scienza e natura",
    "Tecnologia/IA/Futuro": "Scienza e natura",
    "Matematica/Paradossi logici": "Scienza e natura",
    "Ambiente/Clima/Natura": "Scienza e natura",
}

def pick_intelligent_topic() -> tuple[str, str]:
    """Sceglie prima una famiglia editoriale, poi una categoria e un topic."""
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

    recent_families = {
        EDITORIAL_FAMILIES.get(category, category) for category in recent_categories[:2]
    }
    available_families = [
        family for family in dict.fromkeys(EDITORIAL_FAMILIES.values())
        if family not in recent_families
    ]
    if not available_families:
        available_families = list(dict.fromkeys(EDITORIAL_FAMILIES.values()))
    chosen_family = random.choice(available_families)
    available_cats = [
        category for category in TOPIC_IDEAS
        if EDITORIAL_FAMILIES.get(category) == chosen_family
        and category not in recent_categories
    ]
    if not available_cats:
        available_cats = [
            category for category in TOPIC_IDEAS
            if EDITORIAL_FAMILIES.get(category) == chosen_family
        ]

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
        "id": "",
        "titolo": "Generico",
        "file": None,
        "argomenti": [],
        "pitch": "Se ti interessano argomenti simili, fai un salto sul mio profilo."
    }


def find_ebook_by_id(ebook_id: str) -> dict:
    return ebook_to_rag(get_ebook(ebook_id))


def pick_promo_ebook_topic() -> tuple[str, str, str]:
    """Sceglie prima il prodotto e poi un concetto realmente pertinente."""
    ebooks = EBOOKS_DB
    recent_ids: list[str] = []
    try:
        from modules.feedback_loop import get_recent_published
        recent_ids = [
            entry.get("resource_id", "")
            for entry in get_recent_published(30)
            if entry.get("platform") == "tiktok"
            and entry.get("mode") == "promo"
            and entry.get("resource_id")
        ][-12:]
    except (ImportError, OSError, ValueError):
        recent_ids = []

    last_id = recent_ids[-1] if recent_ids else ""
    weights = []
    for ebook in ebooks:
        uses = recent_ids.count(ebook["id"])
        diversity = max(0.4, 1.0 - uses * 0.16)
        if ebook["id"] == last_id:
            diversity *= 0.15
        weights.append(ebook["social_weight"] * diversity)
    ebook = random.choices(ebooks, weights=weights, k=1)[0]
    topic = random.choice(ebook["promo_topics"])
    return f"Ebook/{ebook['id']}", topic, ebook["id"]

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

    angles_path = Path("used_news_history.jsonl")
    recent_angles: list[str] = []
    if angles_path.exists():
        for line in angles_path.read_text(encoding="utf-8").splitlines()[-12:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            angle = str(entry.get("angolo_narrativo", "")).strip()
            if angle and angle.upper() not in {"SKIP", "N/A"}:
                recent_angles.append(angle)
    angles_text = "\n".join(f"- {angle}" for angle in recent_angles[-8:]) or "Nessun angolo recente"

    # REGOLE COMUNI ANTI-DISINFORMAZIONE E SICUREZZA TIKTOK
    anti_misinfo_rules = """
        REGOLE ANTI-DISINFORMAZIONE E VERIDICITÀ (CRITICHE E IMPERATIVE):
        - I manoscritti ebook sono contesto editoriale, MAI prova clinica. Verifica i dati con una fonte primaria e indica il relativo URL in FONTE_NOTIZIA. Nessuna percentuale, autore o durata di un beneficio senza riscontro.
        - Il marchio è ConsciaMente. Preferisci problemi quotidiani, esempi dimostrabili e una piccola azione utile. Le immagini devono mostrare ciò che stai spiegando; evita visual astratti scollegati dal copione.
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

    editorial_contract = """
        CONTRATTO EDITORIALE OBBLIGATORIO:
        Il copione deve sviluppare UN SOLO concetto centrale. Dopo FONTE_NOTIZIA aggiungi sempre, su righe separate:
        FATTO_CENTRALE: una frase verificabile, senza promesse
        TIPO_EVIDENZA: studio, revisione, dato istituzionale o fatto consolidato
        LIMITE_EVIDENZA: cosa non dimostra il dato e quale cautela serve
        ANGOLO_NARRATIVO: l'angolo originale scelto per raccontarlo
        La CTA deve proporre una sola azione concreta, coerente con il contenuto.
    """

    category_guidance = CATEGORY_EDITORIAL_GUIDANCE.get(
        category,
        "Racconta il tema con una fonte verificabile e senza trasformarlo per forza in una scoperta recente.",
    )

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

        ANGOLI NARRATIVI GIÀ USATI (SCEGLI UNA PROSPETTIVA DIVERSA):
        {angles_text}

        {anti_misinfo_rules}
        {editorial_contract}

        REGOLE ANTI-ALLUCINAZIONE (FONDAMENTALI):
        - DEVI basarti STRETTAMENTE sull'estratto dell'ebook fornito. Non inventare cure o terapie.
        - La risorsa collegata è di tipo {ebook.get('delivery_type', 'non specificato')} e la sua pagina è {ebook.get('landing_path', '/links')}.
        - La CTA finale deve essere ESATTAMENTE: {ebook.get('cta_tiktok', 'Scopri di più dal link in bio.')}
        - Non chiamare PDF una risorsa che è soltanto un'anteprima online e non promettere l'invio di messaggi nei DM.

        Scrivi uno script TikTok di 75-90 parole in italiano, pensato per un video di circa 25-35 secondi.
        DEVE SUONARE NATURALE, UMANO E COLLOQUIALE. Elimina il tono robotico o troppo impostato. Parla come se stessi svelando un segreto affascinante a un amico.

        REGOLE FONDAMENTALI PER LA VOCE:
        - Usa frasi brevi, ritmate e dal forte impatto emotivo.
        - In ogni atto inserisci un appiglio visivo concreto: persona, oggetto, luogo, gesto o fenomeno osservabile. Non accumulare spiegazioni astratte.
        - Scrivi TUTTI i numeri in lettere (es. "cento" e non "100").
        - NON USARE MAI simboli speciali, parentesi, o virgolette. Inserisci punti o virgole per far prendere fiato.

        STRUTTURA DEL COPIONE:
        TITOLO: <il tuo titolo accurato qui (max 5 parole)>
        TESTO:
        ATTO 1: Un hook diretto, curioso e conversazionale. Niente formule noiose. Arriva subito al punto (max 14 parole).
        ATTO 2: Spiega il concetto principale estratto dal libro in modo estremamente semplice e visivo (max 28 parole).
        ATTO 3: Svela una conseguenza utile che riguarda la vita di chi guarda (max 30 parole).
        ATTO 4: CTA finale esatta indicata sopra (max 10 parole).
        FONTE_NOTIZIA: <nome dell'ebook (max 1 riga)>
        FATTO_CENTRALE: <fatto verificabile tratto dall'estratto>
        TIPO_EVIDENZA: <estratto del libro, revisione o dato consolidato>
        LIMITE_EVIDENZA: <cosa il contenuto non dimostra>
        ANGOLO_NARRATIVO: <angolo specifico scelto>

        ATTENZIONE CRITICA: LA TUA RISPOSTA DEVE INIZIARE DIRETTAMENTE CON LA PAROLA "TITOLO:".
        NON INSERIRE NESSUN COMMENTO, NESSUNA INTRODUZIONE, NIENTE.
        """
    elif mode == "virale":
        prompt = f"""
        Sei un maestro dello storytelling e analista brillante per TikTok.

        TEMA ASSEGNATO: {topic} (Categoria: {category}). Sii preciso.
        NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
        {history_text}

        ANGOLI NARRATIVI GIÀ USATI (SCEGLI UNA PROSPETTIVA DIVERSA):
        {angles_text}

        MODALITA' VIRALE EDITORIALE:
        STEP 1: Sviluppa esattamente il tema assegnato. Può essere un fatto storico, una pratica culturale, una biografia, un consiglio alimentare o un risultato scientifico: NON deve essere per forza una scoperta o una notizia recente.
        STEP 2: Usa una fonte verificabile adatta al tema: studio o ente scientifico per la scienza, libro o archivio autorevole per le biografie, museo o opera storiografica per la storia, fonte antropologica o istituzionale per culture e rituali.
        STEP 3: Non cambiare il tema per renderlo più scioccante e non inventare collegamenti causali con successo, salute o poteri soprannaturali.

        INDICAZIONE SPECIFICA DELLA CATEGORIA:
        {category_guidance}

        {anti_misinfo_rules}
        {editorial_contract}

        REGOLE ANTI-ALLUCINAZIONE AGGIUNTIVE (CRITICHE):
        - VIETATO combinare istituzioni o studi da domini DIVERSI in una singola fonte fittizia (es. non dire "studio NASA sulla dieta" o "ricerca Harvard-ESA sull'intestino"). Ogni istituzione fa ricerca nel suo dominio.
        - VIETATO inventare nomi di riviste, università, o studi. Se non sei certo della fonte, scrivi "ricercatori dell'Università di [paese reale]" o "uno studio su [journal reale]".
        - REGOLA FONDAMENTALE: Se non puoi sostenere il contenuto con una fonte verificabile adatta alla categoria, non inventarlo. Scegli un fatto documentato sullo stesso tema e indica ente, opera, archivio, documento o studio e, quando disponibile, autore e anno. Sono vietate fonti generiche come "letteratura scientifica consolidata".

        STEP 4: Scrivi un copione TikTok di circa 130 parole in italiano basato su questo contenuto. Deve essere coinvolgente ma accurato.

        REGOLE FONDAMENTALI PER LA VOCE E IL TESTO:
        - Usa frasi incisive, brevi, come un dialogo intimo e rivelatore.
        - In ogni atto inserisci un appiglio visivo concreto: persona, oggetto, luogo, gesto o fenomeno osservabile. Non accumulare spiegazioni astratte.
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
        ATTO 5 — CTA (10-15 parole MAX): Invito esplicito e semplice a seguire il profilo (es. 'seguimi per altri contenuti simili').
        FONTE_NOTIZIA: <ente e documento o studio verificabile; mai una fonte generica>
        FATTO_CENTRALE: <una sola affermazione verificabile>
        TIPO_EVIDENZA: <studio, revisione o fatto consolidato>
        LIMITE_EVIDENZA: <cosa il dato non dimostra>
        ANGOLO_NARRATIVO: <prospettiva originale, non clickbait>

        ATTENZIONE CRITICA: LA TUA RISPOSTA DEVE INIZIARE DIRETTAMENTE CON LA PAROLA "TITOLO:".
        NON INSERIRE NESSUN COMMENTO, NESSUNA INTRODUZIONE, NIENTE. RISPONDI SOLO CON IL COPIONE NEL FORMATO ESATTO RICHIESTO.
        """
    else: # mode == "bastian"
        prompt = f"""
        Sei un maestro dello storytelling e analista brillante per TikTok.

        TEMA ASSEGNATO: {topic} (Categoria: {category}). Sii preciso.
        NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
        {history_text}

        ANGOLI NARRATIVI GIÀ USATI (SCEGLI UNA PROSPETTIVA DIVERSA):
        {angles_text}

        MODALITA' PROSPETTIVA INASPETTATA:
        STEP 1: Parti da un luogo comune reale sul tema e aggiungi una sfumatura documentata che normalmente viene trascurata.
        STEP 2: Usa il tipo di fonte adatto al contenuto. Non inventare controversie, nemici, bugie collettive o complotti.

        INDICAZIONE SPECIFICA DELLA CATEGORIA:
        {category_guidance}

        {anti_misinfo_rules}
        {editorial_contract}

        STEP 4: Scrivi un copione TikTok di circa 130 parole usando una prospettiva inaspettata, equilibrata e utile.
        Lo spettatore deve pensare "non l'avevo considerato", non sentirsi attaccato o ingannato. La conclusione deve aggiungere contesto, non capovolgere artificialmente il fatto iniziale.

        TONO OBBLIGATORIO:
        - Calmo, intelligente, curioso e rispettoso.
        - Vietati: "ti hanno mentito", "tutti sbagliano", "enorme bugia", "la scienza è rotta", "peggiore della tua vita", insulti, paura e superiorità morale.
        - Non usare parole come "distrugge", "annienta", "spietato" o "sconvolgente" per rendere forte un'affermazione debole.
        - Non attribuire a una singola abitudine il successo di una persona.

        REGOLE FONDAMENTALI PER LA VOCE E IL TESTO:
        - Usa frasi incisive, brevi.
        - In ogni atto inserisci un appiglio visivo concreto: persona, oggetto, luogo, gesto o fenomeno osservabile. Non accumulare spiegazioni astratte.
        - Scrivi TUTTI i numeri in lettere.
        - NON USARE MAI simboli speciali, parentesi, o virgolette.
        - Inserisci punti o virgole per far prendere fiato.

        STRUTTURA OBBLIGATORIA DEL COPIONE (5 ATTI — RISPETTA QUESTA STRUTTURA ESATTA):
        TITOLO: <il tuo titolo accurato qui (max 5 parole)>
        TESTO:
        ATTO 1 — HOOK (15-20 parole MAX): Un dettaglio inaspettato o una domanda magnetica che blocca lo scroll. VIETATO INIZIARE CON "Pensiamo spesso che", "Tutti credono che" o formule simili.
        ATTO 2 — CONTESTO (25-30 parole): Presenta rapidamente il luogo comune in modo visivo e diretto.
        ATTO 3 — PROSPETTIVA (40-50 parole): Introduci la sfumatura con una fonte verificata adatta alla categoria. NON trasformare correlazioni in causalità.
        ATTO 4 — CONSEGUENZA (15-20 parole): Spiega cosa cambia nella comprensione del tema, senza dichiarare falso tutto ciò che precede.
        ATTO 5 — CTA (10-15 parole): Azione specifica da fare ORA. INIZIA SEMPRE con uno di questi verbi: Prova, Scopri, Leggi, Salva, Commenta, Usa.
        FONTE_NOTIZIA: <nome della fonte verificata della notizia reale (max 1 riga)>
        FATTO_CENTRALE: <una sola affermazione verificabile>
        TIPO_EVIDENZA: <studio, revisione o fatto consolidato>
        LIMITE_EVIDENZA: <cosa il dato non dimostra>
        ANGOLO_NARRATIVO: <sfumatura inaspettata, documentata e non aggressiva>

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
    ab_prompt = f"""Hai questo ATTO 1 (hook) per un video TikTok divulgativo sul tema '{topic}':
HOOK A: {hook_a}

Scrivi UNA SOLA frase alternativa di 15-20 parole massimo per l'ATTO 1, con un angolo completamente diverso.
NON iniziare MAI con 'Sapevi che', 'Quello che pensi', o formule noiose. Usa un tono narrativo, accurato e diretto. Non trasformare il tema in una nuova scoperta, un segreto o un paradosso se il contenuto originale non lo è.
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

    metadata = extract_metadata(script_text)

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    entry = {
        "timestamp": timestamp,
        "category": category,
        "fonte_notizia": fonte,
        "fatto_centrale": metadata.get("FATTO_CENTRALE", ""),
        "tipo_evidenza": metadata.get("TIPO_EVIDENZA", ""),
        "limite_evidenza": metadata.get("LIMITE_EVIDENZA", ""),
        "angolo_narrativo": metadata.get("ANGOLO_NARRATIVO", ""),
    }
    with open("used_news_history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # Compatibilità con i prompt e gli script cron esistenti.
    with open("used_news_history.txt", "a", encoding="utf-8") as f:
        f.write(f"CAT: {category} | FONTE_NOTIZIA: {fonte}\n")


def save_published_history(category: str, topic: str, fonte: str, metadata: dict) -> None:
    """Registra nello storico anti-ripetizione soltanto un video pubblicato."""
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "category": category,
        "topic": topic,
        "fonte_notizia": fonte,
        "fatto_centrale": metadata.get("FATTO_CENTRALE", ""),
        "tipo_evidenza": metadata.get("TIPO_EVIDENZA", ""),
        "limite_evidenza": metadata.get("LIMITE_EVIDENZA", ""),
        "angolo_narrativo": metadata.get("ANGOLO_NARRATIVO", ""),
    }
    with open("used_news_history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    with open("used_news_history.txt", "a", encoding="utf-8") as f:
        f.write(f"CAT: {category} | TOPIC: {topic} | FONTE_NOTIZIA: {fonte}\n")

def main():
    parser = argparse.ArgumentParser(description="RAG Script Generator per TikTok (Gemini API)")
    parser.add_argument("--topic", default="auto", help="Argomento specifico, oppure 'auto' per scegliere automaticamente")
    parser.add_argument("--category", default="Manuale", help="Categoria del topic (se nota)")
    parser.add_argument("--output", default="scripts/script_generato.txt", help="Dove salvare lo script")
    parser.add_argument("--mode", default="promo", choices=["promo", "virale", "bastian"], help="Modalità video")
    parser.add_argument("--ebook-id", default="", help="Ebook canonico da usare in modalità promo")
    parser.add_argument("--force-new", action="store_true", help="Forza rigenerazione")
    args = parser.parse_args()

    if args.topic.lower() == "auto":
        category, topic = pick_intelligent_topic()
        console.print(f"[green]Topic scelto automaticamente: {topic} (Categoria: {category})[/]")
    else:
        topic = args.topic
        category = args.category

    # 1. Recupero (Retrieval) dell'ebook giusto
    ebook = find_ebook_by_id(args.ebook_id) if args.ebook_id else find_best_ebook(topic)

    # 2. Generazione (Augmented Generation)
    script_text = generate_tiktok_script(topic, category, ebook, args.mode)

    # 2b. A/B Hook Test: testa e sostituisce l'hook se ne esiste uno più forte (~15s extra)
    if "Errore di Generazione" not in script_text:
        console.print("[cyan]⚡ A/B Hook Test in corso...[/]")
        script_text = ab_test_hook(topic, script_text)

    # 3. Salvataggio su file del copione. Lo storico viene aggiornato soltanto
    # dopo che agente_tiktok conferma la pubblicazione.
    out_path = Path(args.output)
    out_path.parent.mkdir(exist_ok=True, parents=True)

    ebook_filename = ebook["file"].name if ebook["file"] else ""
    final_text = script_text + f"\nEBOOK_FILE: {ebook_filename}"

    out_path.write_text(final_text, encoding="utf-8")

    console.print(f"[bold green]✓ Script salvato in {out_path}[/]")
    console.print(f"Anteprima:\n[dim]{script_text}[/dim]")

if __name__ == "__main__":
    main()
