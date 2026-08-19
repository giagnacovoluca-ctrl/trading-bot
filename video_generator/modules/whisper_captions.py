import os
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from rich.console import Console

import config

console = Console()
_model = None

def get_whisper_model():
    """Carica il modello Whisper in memoria se non è già caricato."""
    global _model
    if _model is None:
        console.print("[cyan]Caricamento modello Whisper (versione base, gratuita)...[/]")
        import whisper_timestamped as whisper
        # 'base' è un buon compromesso tra velocità e precisione.
        _model = whisper.load_model("base")
    return _model

def _caption_font_path() -> str:
    p = Path(config.CAPTION_FONT)
    if p.exists():
        return str(p)
    return "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"

def _render_word_highlight(words: list[str], current_word_index: int, video_w: int, font_size: int = 60):
    """
    Renderizza una riga di testo, colorando di GIALLO solo la parola corrente
    e di BIANCO le altre (Hormozi style). Aggiunge emoji in automatico.
    """
    EMOJI_MAP = {
        "SOLDI": "💰", "MENTE": "🧠", "ATTENZIONE": "⚠️", "SCOPERTA": "🔍",
        "SEGRETO": "🤫", "ENERGIA": "⚡", "CERVELLO": "🧠", "FUOCO": "🔥",
        "STOP": "🛑", "CUORE": "❤️", "TEMPO": "⏳", "DENARO": "💶",
        "UNIVERSO": "🌌", "MONDO": "🌍", "VIBRAZIONI": "✨",
        "VITA": "🌱", "SCIENZA": "🔬", "FISICA": "⚛️", "QUANTISTICA": "⚛️"
    }
    
    font_path = _caption_font_path()
    try:
        font = ImageFont.truetype(font_path, font_size)
    except:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", font_size)
        except:
            font = ImageFont.load_default()
            
    try:
        emoji_font = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", int(font_size * 0.9))
    except:
        emoji_font = font

    # Arricchiamo le parole con le emoji
    enriched_words = []
    for w in words:
        clean_w = ''.join(e for e in w if e.isalnum()).upper()
        if clean_w in EMOJI_MAP:
            enriched_words.append(w + " " + EMOJI_MAP[clean_w])
        else:
            enriched_words.append(w)

    line_str = " ".join(enriched_words)
    dummy_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = dummy_draw.textbbox((0, 0), line_str, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    padding_x, padding_y = 50, 30
    box_w = int(min(video_w - 60, tw + padding_x * 2))
    box_h = int(th + padding_y * 2)

    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Sfondo scuro semi-trasparente per far risaltare il testo
    draw.rounded_rectangle([0, 0, box_w, box_h], radius=24, fill=(15, 23, 42, 230), outline=(250, 204, 21, 255), width=5)

    # Calcolo X iniziale per centrare la frase
    start_x = int((box_w - tw) // 2)
    ty = int((box_h - th) // 2 - bbox[1])
    
    current_x = start_x
    for i, w in enumerate(enriched_words):
        is_current = (i == current_word_index)
        # Hormozi style: Giallo forte per la parola pronunciata, Bianco per le altre
        color = (250, 204, 21, 255) if is_current else (255, 255, 255, 255)
        
        # Gestiamo il testo e l'eventuale emoji separatamente
        parts = w.split(" ")
        base_word = parts[0]
        emoji_char = parts[1] if len(parts) > 1 else ""
        
        w_bbox = dummy_draw.textbbox((0, 0), base_word, font=font)
        word_w = w_bbox[2] - w_bbox[0]
        
        if is_current:
            # Effetto POP: disegniamo un'ombra più marcata e uno stroke
            # Ombra
            draw.text((current_x + 4, ty + 4), base_word, font=font, fill=(0, 0, 0, 150))
            # Testo con stroke
            draw.text((current_x, ty), base_word, font=font, fill=color, stroke_width=3, stroke_fill="black")
        else:
            # Testo normale
            draw.text((current_x, ty), base_word, font=font, fill=color, stroke_width=1, stroke_fill="black")
            
        current_x += word_w
        
        # Disegno l'emoji se presente (usando il font emoji)
        if emoji_char:
            # Aggiungo un piccolo spazio
            current_x += 10
            
            # Leggero pop per l'emoji se è la parola corrente
            emoji_y = ty + 2 if is_current else ty + 5
            
            try:
                draw.text((current_x, emoji_y), emoji_char, font=emoji_font, embedded_color=True)
            except:
                draw.text((current_x, emoji_y), emoji_char, font=emoji_font, fill=color)
            
            e_bbox = dummy_draw.textbbox((0, 0), emoji_char, font=emoji_font)
            current_x += (e_bbox[2] - e_bbox[0])
            
        # Spazio finale
        space_bbox = dummy_draw.textbbox((0, 0), " ", font=font)
        current_x += (space_bbox[2] - space_bbox[0])

    return np.array(img)


def generate_dynamic_captions(audio_path: str, video_w: int, video_h: int):
    """
    Genera i sottotitoli dinamici (ImageClip) parola per parola
    interrogando il modello locale Whisper.
    """
    import whisper_timestamped as whisper
    from moviepy.editor import ImageClip

    model = get_whisper_model()
    console.print(f"[cyan]Estrazione timestamp parole da: {audio_path}[/]")
    
    audio = whisper.load_audio(audio_path)
    try:
        result = whisper.transcribe(model, audio, language="it")
    except Exception as e:
        console.print(f"[yellow]Errore Whisper ({e}), riprovo con naive_approach=True...[/]")
        result = whisper.transcribe(model, audio, language="it", naive_approach=True)
    
    captions = []
    MAX_WORDS_PER_LINE = 3
    
    for segment in result.get("segments", []):
        words = segment.get("words", [])
        if not words: continue
        
        # Dividiamo i segmenti lunghi in blocchi di 4 parole massimo
        chunks = [words[i:i + MAX_WORDS_PER_LINE] for i in range(0, len(words), MAX_WORDS_PER_LINE)]
        
        for chunk in chunks:
            chunk_texts = [w["text"].upper() for w in chunk]
            
            # Per ogni blocco di 4 parole, generiamo le singole frame animate
            for i, word in enumerate(chunk):
                start = word["start"]
                end = word["end"]
                
                # Renderizza l'immagine con solo la parola `i` evidenziata
                img_np = _render_word_highlight(chunk_texts, i, video_w)
                
                # Se c'è spazio vuoto tra una parola e l'altra, espandiamo la durata
                # per evitare che il testo "lampeggi" o scompaia.
                if i < len(chunk) - 1:
                    end = max(end, chunk[i+1]["start"])
                elif chunk == chunks[-1] and segment != result["segments"][-1]:
                    # Se è l'ultima parola del segmento, teniamo per un decimo di sec extra
                    end += 0.1
                
                clip = (
                    ImageClip(img_np, transparent=True)
                    .set_start(start)
                    .set_end(end)
                    .set_position(("center", int(video_h * 0.70)))
                )
                captions.append(clip)
                
    console.print(f"[bold green]✓ Generate {len(captions)} word-highlights dinamiche![/]")
    return captions
