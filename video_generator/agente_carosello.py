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

console = Console()

EBOOKS = [
    "Cibo/Salute",
    "Meditazione",
    "Integratori",
    "Acqua/Idratazione",
    "Epigenetica/DNA",
    "Nervo Vago/Stress"
]

CATEGORIES_MAP = {
    "Cibo/Salute": "cibo_salute",
    "Integratori": "cibo_salute",
    "Meditazione": "nervo_vago",
    "Nervo Vago/Stress": "nervo_vago",
    "Acqua/Idratazione": "acqua_idratazione",
    "Epigenetica/DNA": "epigenetica"
}

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


def valida_output(data: object, format_type: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Il revisore deve restituire un oggetto JSON")
    caption = data.get("caption")
    if not isinstance(caption, str) or not caption.strip():
        raise ValueError("Caption mancante")

    if format_type == "aesthetic":
        text = data.get("testo_schermo")
        if not isinstance(text, str) or not text.strip() or len(text.split()) > 15:
            raise ValueError("testo_schermo deve contenere da 1 a 15 parole")
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
    return {"slides": normalized, "caption": caption.strip()}

def estrai_json(testo: str) -> str:
    """Estrae l'array JSON o l'oggetto JSON dal testo grezzo di AGY."""
    # Cerca il blocco json tra i backticks
    match = re.search(r'```json\s*([\s\S]*?)\s*```', testo)
    if match:
        return match.group(1)

    # Se non ci sono backticks, cerca la prima parentesi quadra o graffa
    match_array = re.search(r'\[\s*\{.*\}\s*\]', testo, re.DOTALL)
    if match_array:
        return match_array.group(0)

    match_obj = re.search(r'\{\s*".*\}\s*', testo, re.DOTALL)
    if match_obj:
        return match_obj.group(0)

    return testo

def genera_contenuto(mode: str) -> tuple[str, str, str]:
    """Genera il contenuto (JSON) a seconda della modalità."""

    prompt_base = "Sei un social media manager e copywriter di altissimo livello. "

    if mode == "promo":
        ebook = random.choice(EBOOKS)
        category = CATEGORIES_MAP[ebook]
        prompt = prompt_base + f"""
Genera un carosello TikTok/Instagram (5 o 6 slide) per promuovere l'Ebook '{ebook}'.
REGOLE: Spiega un CONCETTO SPECIFICO tratto dal libro in modo intellettualmente stimolante.
Nell'ultima slide usa ESATTAMENTE questa Call to Action: 'Commenta MANUALE e te lo mando nei DM'. Non dire link in bio.

ATTENZIONE FONDAMENTALE SULLA SCRITTURA: I caroselli non hanno voce narrante, l'utente leggerà SOLO la chiave 'overlay_text'. L'overlay_text deve formare un discorso logico, coerente e fluido tra le slide, MA DEVE ESSERE BREVE, CONCISO E DI FORTE IMPATTO. Usa un tono molto SERIO, PROFESSIONALE E AUTOREVOLE. Evita "muri di testo" (massimo 15-20 parole per slide). Niente battutine o toni infantili. Spiega concetti complessi in frasi brevi e taglienti.

Restituisci ESATTAMENTE un file JSON formattato così, senza aggiungere altro testo fuori dal JSON:
{{
  "slides": [
    {{"slide_number": 1, "visual_idea": "...", "overlay_text": "..."}},
    ...
  ],
  "caption": "La tua caption persuasiva molto lunga con CTA a commentare MANUALE per ricevere il link in DM e hashtag."
}}
"""
    elif mode == "virale":
        cat = random.choice(CATEGORIES_VIRALE)
        category = "default"
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
        ebook = random.choice(EBOOKS)
        category = CATEGORIES_MAP[ebook]
        prompt = prompt_base + f"""
Devi promuovere il mio libro: '{ebook}' (Stile Dark Academia / Quiet Luxury).
REGOLE:
1. Crea una SINGOLA FRASE ad altissimo impatto (max 15 parole) che faccia riflettere profondamente. Sarà il testo a schermo nel video. Tagliente, cinica o rivelatoria.
2. Crea una DESCRIZIONE PER INSTAGRAM lunga e accattivante. Inizia spiegando la frase, distruggi un falso mito e finisci ESATTAMENTE con questa CTA: 'Scrivi la parola GUIDA nei commenti e ti manderò il manuale nei DM.' Non parlare di link in bio.

Restituisci ESATTAMENTE un file JSON formattato così:
{{
  "testo_schermo": "...",
  "caption": "..."
}}
"""
    else:
        return "", "", ""

    return prompt, category, "aesthetic" if mode == "aesthetic" else "carosello"

def revisione_logica(testo_json: str, format_type: str) -> tuple[bool, str, str]:
    """L'Agente Direttore revisiona il JSON generato."""

    prompt = f"""Sei il Direttore Editoriale. Controlla questo output JSON generato per un {format_type}.
JSON DA REVISIONARE:
{testo_json}

Il tuo compito è scansionarlo e dirmi se rispetta le regole.
Regole per 'carosello': L'overlay_text deve essere LOGICO e CONSEQUENZIALE dalla slide 1 alla 6. NON ci devono essere muri di testo (frasi brevi e taglienti). Se il JSON è corrotto, boccialo.
Regole per 'aesthetic': Il testo_schermo deve essere cortissimo (max 15 parole) e d'impatto.

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
    for f in ["scripts/slides_carosello.json", "scripts/ig_caption.txt", "scripts/ig_aesthetic_data.json"]:
        if os.path.exists(f):
            os.remove(f)

    MAX_RETRIES = 3
    success = False

    prompt, category, format_type = genera_contenuto(args.mode)

    for tentativo in range(1, MAX_RETRIES + 1):
        console.print(f"[bold cyan]Tentativo {tentativo}/{MAX_RETRIES} per generazione {args.mode}[/]")

        raw_output = chiama_agy(prompt)
        json_raw = estrai_json(raw_output)

        try:
            parsed = json.loads(json_raw) # Verifica validità base

            # Passa all'Agente Revisore
            is_valid, msg, json_revisionato = revisione_logica(json_raw, format_type)

            if is_valid:
                final_json = valida_output(json.loads(json_revisionato), format_type)
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

        console.print("[magenta]Avvio pipeline crea_carosello.py...[/]")
        command = [sys.executable, "crea_carosello.py"]
        if args.no_publish:
            command.append("--no-publish")
        subprocess.run(command, check=True)
        # (Il caricamento su IG avviene già dentro crea_carosello.py)

    else:
        # Formato aesthetic
        with open("scripts/temp_aesthetic_text.txt", "w", encoding="utf-8") as f:
            f.write(final_json["testo_schermo"])
        with open("scripts/ig_caption.txt", "w", encoding="utf-8") as f:
            f.write(final_json["caption"])

        console.print("[magenta]Avvio pipeline crea_ig_aesthetic.py...[/]")
        subprocess.run([sys.executable, "crea_ig_aesthetic.py", "--text", final_json["testo_schermo"], "--category", category, "--out", "output/aesthetic_reel.mp4"], check=True)
        if args.no_publish:
            console.print("[yellow]Modalità --no-publish: Reel generato senza upload.[/]")
            return
        console.print("[magenta]Avvio caricamento IG...[/]")
        subprocess.run([sys.executable, "step4_pubblica_ig_api.py", "--video", "output/aesthetic_reel.mp4"], check=True)

    console.print("[bold green]CICLO COMPLETATO CON SUCCESSO![/]")

if __name__ == "__main__":
    main()
