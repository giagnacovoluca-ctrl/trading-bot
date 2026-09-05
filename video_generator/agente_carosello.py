import os
import sys
import json
import re
import time
import random
import argparse
import subprocess
from pathlib import Path
from rich.console import Console
from modules.ebook_catalog import get_ebook_by_title, load_ebook_catalog
from modules.script_quality import validate_publication_text

console = Console()

EBOOK_CATALOG = load_ebook_catalog()
EBOOKS = [book["title"] for book in EBOOK_CATALOG]

# Prior editoriali iniziali ricavati dai primi risultati Instagram disponibili.
# La meditazione ha mostrato il miglior rapporto interazioni/copertura; gli altri
# temi mantengono comunque abbastanza peso da continuare a raccogliere dati.
AESTHETIC_TOPIC_PRIORS = {
    book["title"]: float(book["socialWeight"])
    for book in EBOOK_CATALOG
}

AESTHETIC_CTA_PDF = "Ricevi il PDF gratuito dal link in bio."
AESTHETIC_CTA_PREVIEW = "Leggi gratuitamente l'anteprima dal link in bio."
AESTHETIC_MIN_SCREEN_WORDS = 7
AESTHETIC_MAX_SCREEN_WORDS = 12
AESTHETIC_MIN_CAPTION_CHARS = 450
AESTHETIC_MAX_CAPTION_CHARS = 900
AESTHETIC_CLICHES = (
    "bugia",
    "truffa",
    "lusso",
    "élite",
    "elite",
    "spietat",
    "mediocrità",
    "domina",
)

CATEGORIES_MAP = {book["title"]: book["category"] for book in EBOOK_CATALOG}

CATEGORIES_VIRALE = [
    "neuroscienze",
    "fisica/spazio/astronomia",
    "biologia/evoluzione",
    "tecnologia/IA/futuro",
    "economia comportamentale",
    "sociologia/antropologia"
]

def chiama_agy(prompt: str) -> str:
    """Chiama AGY via subprocess e restituisce l'output."""
    try:
        result = subprocess.run(
            ['agy', '--dangerously-skip-permissions', '--print', prompt],
            text=True, capture_output=True, timeout=120, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        raise RuntimeError(f"Errore comunicazione con AGY: {e}") from e


def valida_output(
    data: object,
    format_type: str,
    expected_cta: str = "",
) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Il revisore deve restituire un oggetto JSON")
    caption = data.get("caption")
    if not isinstance(caption, str) or not caption.strip():
        raise ValueError("Caption mancante")
    visible_text = caption + ' ' + str(data.get('testo_schermo', '')) + ' ' + ' '.join(str(s.get('overlay_text', '')) for s in data.get('slides', []) if isinstance(s, dict))
    issues = validate_publication_text(visible_text)
    if issues:
        raise ValueError('; '.join(issues))

    if format_type == "aesthetic":
        text = data.get("testo_schermo")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("testo_schermo mancante")
        word_count = len(text.split())
        if not AESTHETIC_MIN_SCREEN_WORDS <= word_count <= AESTHETIC_MAX_SCREEN_WORDS:
            raise ValueError(
                f"testo_schermo deve contenere da {AESTHETIC_MIN_SCREEN_WORDS} "
                f"a {AESTHETIC_MAX_SCREEN_WORDS} parole"
            )
        caption_length = len(caption.strip())
        if not AESTHETIC_MIN_CAPTION_CHARS <= caption_length <= AESTHETIC_MAX_CAPTION_CHARS:
            raise ValueError(
                f"caption aesthetic deve contenere da {AESTHETIC_MIN_CAPTION_CHARS} "
                f"a {AESTHETIC_MAX_CAPTION_CHARS} caratteri"
            )
        if expected_cta and expected_cta.lower() not in caption.lower():
            raise ValueError("caption aesthetic senza la CTA richiesta")
        combined = f"{text} {caption}".lower()
        cliche_count = sum(combined.count(term) for term in AESTHETIC_CLICHES)
        if cliche_count > 1:
            raise ValueError("caption aesthetic troppo simile al vecchio stile elitario/catastrofico")
        return {"testo_schermo": text.strip(), "caption": caption.strip()}

    slides = data.get("slides")
    if not isinstance(slides, list) or not 5 <= len(slides) <= 6:
        raise ValueError("Il carosello deve contenere da 5 a 6 slide")
    normalized = []
    for index, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            raise ValueError(f"Slide {index} non valida")
        overlay = slide.get("overlay_text")
        if not isinstance(overlay, str) or not overlay.strip() or len(overlay.split()) > 20:
            raise ValueError(f"overlay_text non valido nella slide {index}")
        normalized.append({**slide, "slide_number": index, "overlay_text": overlay.strip()})
    if expected_cta:
        if expected_cta.lower() not in caption.lower():
            raise ValueError("caption carosello senza la CTA richiesta")
        if expected_cta.lower() not in normalized[-1]["overlay_text"].lower():
            raise ValueError("ultima slide senza la CTA richiesta")
    return {"slides": normalized, "caption": caption.strip()}

def estrai_json(testo: str) -> str:
    """Estrae JSON valido dal testo di AGY, privilegiando l'oggetto completo."""
    decoder = json.JSONDecoder()
    array_fallback = []

    # Prova prima il contenuto dei blocchi Markdown, poi l'intera risposta.
    blocchi = re.findall(r'```(?:json)?\s*([\s\S]*?)\s*```', testo, re.IGNORECASE)
    sorgenti = blocchi + [testo]
    for sorgente in sorgenti:
        candidati = []
        for match in re.finditer(r'[\{\[]', sorgente):
            try:
                valore, fine = decoder.raw_decode(sorgente[match.start():])
            except json.JSONDecodeError:
                continue
            inizio = match.start()
            frammento = sorgente[inizio:inizio + fine]
            candidati.append((inizio, inizio + fine, valore, frammento))

        intervalli_array = [
            (inizio, fine) for inizio, fine, valore, _ in candidati
            if isinstance(valore, list)
        ]
        for inizio, _, valore, frammento in candidati:
            if isinstance(valore, dict) and not any(
                array_inizio < inizio < array_fine
                for array_inizio, array_fine in intervalli_array
            ):
                return frammento
        array_fallback.extend(
            frammento for _, _, valore, frammento in candidati
            if isinstance(valore, list)
        )

    # Mantiene compatibilita con eventuali chiamanti che richiedono un array.
    return array_fallback[0] if array_fallback else testo.strip()

def _recent_aesthetic_entries(limit: int = 12) -> list[dict]:
    try:
        from modules.feedback_loop import get_recent_published
        return [
            entry for entry in get_recent_published(limit * 3)
            if entry.get("platform") == "instagram"
            and entry.get("mode") == "aesthetic"
        ][-limit:]
    except (ImportError, OSError, ValueError):
        return []


def scegli_ebook_aesthetic() -> str:
    """Bilancia prior iniziali, insight Instagram e varietà recente."""
    recent_topics = [
        entry.get("topic")
        for entry in _recent_aesthetic_entries()
        if entry.get("topic")
    ]
    last_topic = recent_topics[-1] if recent_topics else ""
    try:
        from modules.feedback_loop import get_topic_weights
        categories = sorted(set(CATEGORIES_MAP.values()))
        performance_weights = get_topic_weights(categories)
        # I pesi sono normalizzati (somma 1): li riportiamo attorno a 1 per
        # usarli come moltiplicatore dei prior editoriali invece di annullarli.
        performance_multiplier = {
            category: performance_weights.get(category, 1 / len(categories)) * len(categories)
            for category in categories
        }
    except (ImportError, OSError, ValueError):
        performance_multiplier = {category: 1.0 for category in CATEGORIES_MAP.values()}
    weights = []
    for ebook in EBOOKS:
        uses = recent_topics.count(ebook)
        diversity_factor = max(0.35, 1.0 - (uses * 0.16))
        if ebook == last_topic:
            diversity_factor *= 0.15
        weights.append(
            AESTHETIC_TOPIC_PRIORS[ebook]
            * performance_multiplier.get(CATEGORIES_MAP[ebook], 1.0)
            * diversity_factor
        )
    return random.choices(EBOOKS, weights=weights, k=1)[0]


def _recent_aesthetic_hooks(limit: int = 8) -> list[str]:
    return [
        entry.get("hook_title", "").strip()
        for entry in _recent_aesthetic_entries(limit)
        if entry.get("hook_title", "").strip()
    ]


def genera_contenuto(mode: str) -> tuple[str, str, str, str, str]:
    """Genera il contenuto (JSON) a seconda della modalità."""

    prompt_base = "Sei un social media manager e copywriter di altissimo livello. "

    if mode == "promo":
        ebook = random.choice(EBOOKS)
        ebook_data = get_ebook_by_title(ebook)
        category = ebook_data["category"]
        topic = ebook
        promo_cta = ebook_data["ctaTikTok"]
        expected_cta = promo_cta
        prompt = prompt_base + f"""
Genera un carosello TikTok/Instagram (5 o 6 slide) per promuovere l'Ebook '{ebook}'.
REGOLE: Spiega un CONCETTO SPECIFICO tratto dal libro in modo intellettualmente stimolante.
Nell'ultima slide usa ESATTAMENTE questa Call to Action: '{promo_cta}' Non promettere invii automatici nei DM e non chiamare PDF una semplice anteprima.

ATTENZIONE FONDAMENTALE SULLA SCRITTURA: I caroselli non hanno voce narrante, l'utente leggerà SOLO la chiave 'overlay_text'. L'overlay_text deve formare un discorso logico, coerente e fluido tra le slide, MA DEVE ESSERE BREVE, CONCISO E DI FORTE IMPATTO. Usa un tono molto SERIO, PROFESSIONALE E AUTOREVOLE. Evita "muri di testo" (massimo 15-20 parole per slide). Niente battutine o toni infantili. Spiega concetti complessi in frasi brevi e taglienti.

Restituisci ESATTAMENTE un file JSON formattato così, senza aggiungere altro testo fuori dal JSON:
{{
  "slides": [
    {{"slide_number": 1, "visual_idea": "...", "overlay_text": "..."}},
    ...
  ],
  "caption": "La tua caption persuasiva con la stessa CTA dell'ultima slide, più hashtag pertinenti."
}}
"""
    elif mode == "virale":
        cat = random.choice(CATEGORIES_VIRALE)
        category = "default"
        topic = cat
        expected_cta = ""
        prompt = prompt_base + f"""
Genera un carosello TikTok/Instagram ipnotico (5 o 6 slide) sul tema: {cat}.
REGOLE: Divulgativo, basato su fatti verificabili, senza clickbait falso. Trova un angolo nascosto e spiega in modo logico.
ATTENZIONE FONDAMENTALE SULLA SCRITTURA: I caroselli non hanno voce narrante, l'utente leggerà SOLO la chiave 'overlay_text'. L'overlay_text deve formare un discorso logico, coerente e fluido tra le slide, MA DEVE ESSERE BREVE, CONCISO E DI FORTE IMPATTO. Usa un tono molto SERIO, PROFESSIONALE E AUTOREVOLE. Evita "muri di testo" (massimo 15-20 parole per slide). Spiega concetti complessi in frasi brevi e taglienti.
Nell'ultima slide chiedi un'opinione nei commenti per stimolare l'algoritmo.

Restituisci ESATTAMENTE un file JSON formattato così:
{{
  "slides": [
    {{"slide_number": 1, "visual_idea": "...", "overlay_text": "..."}},
    ...
  ],
  "caption": "La tua caption lunga con hashtag che pone una domanda alla fine."
}}
"""
    elif mode == "bastian":
        category = "default"
        topic = "bastian contrario"
        expected_cta = ""
        prompt = prompt_base + f"""
Genera un carosello in stile Bastian Contrario (sarcastico, diretto, distrugge un falso mito con dati).
ATTENZIONE FONDAMENTALE SULLA SCRITTURA: I caroselli non hanno voce narrante, l'utente leggerà SOLO la chiave 'overlay_text'. L'overlay_text deve formare un discorso logico, coerente e fluido tra le slide, MA DEVE ESSERE BREVE, CONCISO E DI FORTE IMPATTO. Usa un tono molto SERIO, PROFESSIONALE E AUTOREVOLE. Evita "muri di testo" (massimo 15-20 parole per slide). Niente battutine.
Nell'ultima slide chiedi un parere provocatorio ai follower.

Restituisci ESATTAMENTE un file JSON formattato così:
{{
  "slides": [
    {{"slide_number": 1, "visual_idea": "...", "overlay_text": "..."}},
    ...
  ],
  "caption": "Caption molto accesa che stimola il dibattito, con hashtag."
}}
"""
    elif mode == "aesthetic":
        ebook = scegli_ebook_aesthetic()
        ebook_data = get_ebook_by_title(ebook)
        category = ebook_data["category"]
        topic = ebook
        expected_cta = ebook_data["ctaInstagram"]
        delivery_description = (
            "un PDF gratuito inviato via email"
            if ebook_data["deliveryType"] == "pdf_email"
            else "un'anteprima gratuita leggibile subito online, senza registrazione"
        )
        recent_hooks = _recent_aesthetic_hooks()
        avoid_text = json.dumps(recent_hooks, ensure_ascii=False) if recent_hooks else "[]"
        prompt = prompt_base + f"""
Devi creare un Reel Instagram aesthetic di 6 secondi per promuovere il libro: '{ebook}'.
La risorsa offerta nel link in bio è {delivery_description}.
Il Reel mostra una sola immagine, una frase a schermo e non ha voce narrante.

DATI EDITORIALI:
- Nei primi risultati la meditazione ha prodotto più interazioni e salvataggi.
- Il formato breve viene guardato mediamente per 4-5 secondi su 6: mantieni la leggibilità immediata.
- Le CTA artificiali nei commenti non hanno funzionato.

REGOLE:
1. TESTO A SCHERMO: una sola frase di 7-12 parole, concreta, autonoma e comprensibile al primo sguardo. Deve creare tensione o curiosità senza essere vaga.
2. Non usare nel testo o nella caption: "lusso", "élite", "mediocrità", "spietato", "dominare", "truffa". Usa "bugia" al massimo una volta e solo se davvero necessaria.
3. Evita formule già viste come "ti hanno fatto credere", "il vero lusso", "non è per tutti" e "mentre il mondo annega nel caos".
4. CAPTION: 450-900 caratteri, 2-4 paragrafi brevi. Apri sviluppando subito la frase, correggi un solo falso mito con tono autorevole ma non aggressivo e aggiungi un'informazione o un'applicazione concreta.
5. Per salute, biologia e integratori evita diagnosi, promesse, causalità assolute e affermazioni non verificabili. Non presentare integratori o pratiche come cure universali.
6. Chiudi con questa frase ESATTA: '{expected_cta}' Dopo la CTA puoi inserire da 0 a 5 hashtag pertinenti.
7. Non promettere messaggi automatici o invii nei DM.

HOOK RECENTI DA NON RIPETERE NÉ PARAFRASARE TROPPO DA VICINO:
{avoid_text}

Restituisci ESATTAMENTE un file JSON formattato così:
{{
  "testo_schermo": "...",
  "caption": "..."
}}
"""
    else:
        return "", "", "", "", ""

    return prompt, category, "aesthetic" if mode == "aesthetic" else "carosello", topic, expected_cta

def revisione_logica(
    testo_json: str,
    format_type: str,
    selected_topic: str = "",
    expected_cta: str = "",
) -> tuple[bool, str, str]:
    """L'Agente Direttore revisiona il JSON generato."""

    prompt = f"""Sei il Direttore Editoriale. Controlla questo output JSON generato per un {format_type}.
TEMA/PRODOTTO SELEZIONATO: {selected_topic or 'non specificato'}
JSON DA REVISIONARE:
{testo_json}

Il tuo compito è scansionarlo e dirmi se rispetta le regole.
Controlla anche la correttezza: nessun numero clinico senza una fonte precisa e verificata, nessuna promessa biologica derivata soltanto dal libro. Se non puoi verificare un'affermazione, riscrivila come osservazione pratica senza claim sanitario. Ayurveda e numerologia sono tradizioni/strumenti simbolici, non diagnosi o prove scientifiche. Preferisci un problema concreto, un esempio e una piccola azione. Il marchio è ConsciaMente.
Regole per 'carosello': L'overlay_text deve essere LOGICO e CONSEQUENZIALE dalla slide 1 alla 6. NON ci devono essere muri di testo (frasi brevi e taglienti). Se il JSON è corrotto, boccialo.
Regole per 'aesthetic':
- testo_schermo di 7-12 parole, concreto, autonomo e leggibile in pochi secondi;
- caption di 450-900 caratteri, in paragrafi brevi, con un solo concetto centrale;
- CTA obbligatoria: '{expected_cta or 'coerente con il formato'}';
- niente promesse mediche, diagnosi o affermazioni sanitarie assolute;
- non più di una ricorrenza complessiva dei cliché: bugia, truffa, lusso, élite, spietato, mediocrità, dominare;
- il testo deve evitare formule generiche o ripetitive e restare coerente con il libro scelto.

Se è TUTTO PERFETTO e senza errori, restituisci:
ESITO: APPROVATO
JSON: (il json intatto)

Se ci sono difetti, riscrivi TU il JSON corretto e restituisci:
ESITO: CORRETTO
JSON: (il json aggiustato)
"""

    console.print("[dim]...Agente Revisore in azione...[/]")
    risposta = chiama_agy(prompt)

    if "ESITO: APPROVATO" in risposta or "ESITO: CORRETTO" in risposta:
        pulito = estrai_json(risposta)
        try:
            json.loads(pulito)
            return True, "Revisione superata", pulito
        except:
            return False, "JSON rotto dal revisore", ""

    return False, "Bocciato dal revisore", ""

def main():
    parser = argparse.ArgumentParser(description="Orchestratore Caroselli & Aesthetic")
    parser.add_argument("--mode", choices=["promo", "virale", "bastian", "aesthetic"], required=True)
    parser.add_argument("--no-publish", action="store_true", help="Genera gli asset senza caricarli sui social")
    args = parser.parse_args()

    os.chdir(Path(__file__).parent)

    # Pulizia di sicurezza
    for f in [
        "scripts/slides_carosello.json",
        "scripts/tiktok_caption.txt",
        "scripts/ig_caption.txt",
        "scripts/ig_aesthetic_data.json",
    ]:
        if os.path.exists(f):
            os.remove(f)

    MAX_RETRIES = 3
    success = False

    prompt, category, format_type, topic, expected_cta = genera_contenuto(args.mode)

    for tentativo in range(1, MAX_RETRIES + 1):
        console.print(f"[bold cyan]Tentativo {tentativo}/{MAX_RETRIES} per generazione {args.mode}[/]")

        raw_output = chiama_agy(prompt)
        json_raw = estrai_json(raw_output)

        try:
            parsed = json.loads(json_raw) # Verifica validità base

            # Passa all'Agente Revisore
            is_valid, msg, json_revisionato = revisione_logica(
                json_raw, format_type, topic, expected_cta
            )

            if is_valid:
                final_json = valida_output(
                    json.loads(json_revisionato), format_type, expected_cta
                )
                console.print(f"[bold green]OK! Qualità validata dall'Agente Direttore.[/]")
                success = True
                break
            else:
                console.print(f"[yellow]Review fallita: {msg}[/]")
        except Exception as e:
            console.print(f"[yellow]JSON Invalido al tentativo {tentativo}: {e}[/]")

    if not success:
        console.print("[bold red]L'Agente non è riuscito a generare un contenuto valido dopo 3 tentativi. Abbandono.[/]")
        sys.exit(1)

    # Salvataggio Dati
    if format_type == "carosello":
        # Salva slides per crea_carosello.py
        with open("scripts/slides_carosello.json", "w", encoding="utf-8") as f:
            json.dump(final_json["slides"], f, ensure_ascii=False)
        # Salva caption
        with open("scripts/ig_caption.txt", "w", encoding="utf-8") as f:
            f.write(final_json["caption"])
        # TikTok e Instagram devono usare la stessa caption appena revisionata.
        # In assenza di questo file step4_pubblica.py potrebbe leggere una
        # descrizione residua prodotta da un'altra pipeline.
        with open("scripts/tiktok_caption.txt", "w", encoding="utf-8") as f:
            f.write(final_json["caption"])

        console.print("[magenta]Avvio pipeline crea_carosello.py...[/]")
        resource = get_ebook_by_title(topic) if args.mode == "promo" else None
        os.environ["CONSCIA_RESOURCE_ID"] = resource["id"] if resource else ""
        command = [sys.executable, "crea_carosello.py"]
        if args.no_publish:
            command.append("--no-publish")
        subprocess.run(command, check=True)
        # (Il caricamento su IG avviene già dentro crea_carosello.py)
        if not args.no_publish:
            from modules.feedback_loop import log_upload
            first_slide = final_json["slides"][0].get("overlay_text", "Carosello")
            resource = get_ebook_by_title(topic) if args.mode == "promo" else None
            log_upload(
                video_file="output/carosello_finale.mp4",
                hook_title=first_slide,
                category=category,
                mode=f"carosello_{args.mode}",
                quality_score=None,
                fonte="",
                success=True,
                topic=topic,
                platform="tiktok",
                resource_id=resource["id"] if resource else "",
                delivery_type=resource["deliveryType"] if resource else "",
            )

    else:
        # Formato aesthetic
        resource = get_ebook_by_title(topic)
        os.environ["CONSCIA_RESOURCE_ID"] = resource["id"]
        visual_cta = (
            "PDF GRATUITO · LINK IN BIO"
            if resource["deliveryType"] == "pdf_email"
            else "ANTEPRIMA GRATUITA · LINK IN BIO"
        )
        with open("scripts/temp_aesthetic_text.txt", "w", encoding="utf-8") as f:
            f.write(final_json["testo_schermo"])
        with open("scripts/ig_caption.txt", "w", encoding="utf-8") as f:
            f.write(final_json["caption"])

        console.print("[magenta]Avvio pipeline crea_ig_aesthetic.py...[/]")
        subprocess.run([
            sys.executable, "crea_ig_aesthetic.py",
            "--text", final_json["testo_schermo"],
            "--category", category,
            "--cta", visual_cta,
            "--out", "output/aesthetic_reel.mp4",
        ], check=True)
        if args.no_publish:
            console.print("[yellow]Modalità --no-publish: Reel generato senza upload.[/]")
            return
        console.print("[magenta]Avvio caricamento IG...[/]")
        receipt_path = Path("output/instagram_aesthetic_receipt.json")
        receipt_path.unlink(missing_ok=True)
        subprocess.run([
            sys.executable,
            "step4_pubblica_ig_api.py",
            "--video",
            "output/aesthetic_reel.mp4",
            "--script",
            "scripts/temp_aesthetic_text.txt",
            "--mode",
            "aesthetic",
            "--receipt",
            str(receipt_path),
        ], check=True)
        media_id = ""
        try:
            media_id = str(json.loads(receipt_path.read_text(encoding="utf-8"))["media_id"])
        except (OSError, KeyError, json.JSONDecodeError):
            console.print("[yellow]Reel pubblicato, ma ID Meta non disponibile per le metriche.[/]")
        from modules.feedback_loop import log_upload
        log_upload(
            video_file="output/aesthetic_reel.mp4",
            hook_title=final_json["testo_schermo"],
            category=category,
            mode="aesthetic",
            quality_score=None,
            fonte="",
            success=True,
            topic=topic,
            platform="instagram",
            resource_id=resource["id"],
            delivery_type=resource["deliveryType"],
            media_id=media_id,
        )

    console.print("[bold green]CICLO COMPLETATO CON SUCCESSO![/]")

if __name__ == "__main__":
    main()
