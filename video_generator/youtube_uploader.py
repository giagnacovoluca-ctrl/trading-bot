import os
import sys
import pickle
import argparse
from pathlib import Path
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from rich.console import Console

console = Console()

# Definisci gli scope necessari per fare l'upload su YouTube
SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

def get_authenticated_service():
    """Autentica l'utente e restituisce il servizio YouTube."""
    creds = None
    token_file = 'youtube_token.pickle'

    # Se esiste un token salvato, caricalo
    if os.path.exists(token_file):
        with open(token_file, 'rb') as token:
            creds = pickle.load(token)

    # Se le credenziali non sono valide o non esistono, facciamo il login
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            console.print("[dim]Refresh del token YouTube in corso...[/]")
            creds.refresh(Request())
        else:
            if not os.path.exists('client_secrets.json'):
                console.print("[red]Errore:[/] File 'client_secrets.json' non trovato. Devi scaricarlo da Google Cloud Console.")
                sys.exit(1)

            console.print("[cyan]Avvio autenticazione OAuth per YouTube...[/]")
            # Avvia un server locale per intercettare la risposta di Google
            flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)

            # Se siamo su un server remoto (es. Ubuntu headless), usare run_console() invece di run_local_server()
            # creds = flow.run_console()  # <--- Decommentare se non hai interfaccia grafica
            creds = flow.run_local_server(port=0)

        # Salva le credenziali per i futuri avvii automatici (cron)
        with open(token_file, 'wb') as token:
            pickle.dump(creds, token)

    return build('youtube', 'v3', credentials=creds)

def upload_video(youtube, video_path, title, description, tags, category_id="22", privacy_status="public"):
    """Esegue l'upload del video."""
    console.print(f"[cyan]Inizio caricamento su YouTube Shorts:[/] {title}")

    body = {
        'snippet': {
            'title': title,
            'description': description,
            'tags': tags,
            'categoryId': category_id
        },
        'status': {
            'privacyStatus': privacy_status,
            'selfDeclaredMadeForKids': False
        }
    }

    # Inizializza l'upload
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype='video/mp4')

    request = youtube.videos().insert(
        part=','.join(body.keys()),
        body=body,
        media_body=media
    )

    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                console.print(f"[dim]Caricamento: {int(status.progress() * 100)}%[/]")
        except Exception as e:
            console.print(f"[red]Errore durante l'upload:[/] {e}")
            return None

    video_id = response.get('id')
    console.print(f"[bold green]✓ Video caricato con successo! URL: https://youtu.be/{video_id}[/]")
    return video_id

def main():
    parser = argparse.ArgumentParser(description="YouTube Shorts Uploader")
    parser.add_argument("--video", required=True, help="Percorso del file video (.mp4)")
    parser.add_argument("--title", required=True, help="Titolo del video")
    parser.add_argument("--description", default="", help="Descrizione del video")
    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        console.print(f"[red]Video non trovato:[/] {video_path}")
        sys.exit(1)

    # Assicura che il titolo abbia l'hashtag #Shorts (fortemente consigliato da YouTube)
    title = args.title
    if "#shorts" not in title.lower():
         title = f"{title[:80]} #Shorts" # taglia a 80 char per evitare limiti

    tags = ["biohacking", "mindfulness", "salute", "benessere", "consciamente"]

    # 1. Autenticazione
    youtube = get_authenticated_service()

    # 2. Upload
    if upload_video(youtube, str(video_path), title, args.description, tags) is None:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
