import feedparser
import random
import requests
import json
import os

HISTORY_FILE = "used_news_history.json"

def is_news_used(title: str) -> bool:
    if not os.path.exists(HISTORY_FILE):
        return False
    try:
        with open(HISTORY_FILE, "r") as f:
            history = json.load(f)
            return title in history
    except:
        return False

def mark_news_used(title: str):
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                history = json.load(f)
        except:
            pass
    history.append(title)
    # Tieni solo le ultime 100 per non far crescere il file a dismisura
    if len(history) > 100:
        history = history[-100:]
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)

def fetch_hacker_news() -> dict:
    """Non usiamo più Hacker News perché troppo focalizzato su programmazione/tech."""
    raise ValueError("Hacker News disabilitato (fuori tema)")

def fetch_rss_news() -> dict:
    """Recupera una notizia da feed RSS di salute, mente, scienza e benessere."""
    feeds = [
        "https://www.sciencedaily.com/rss/mind_brain.xml",
        "https://www.sciencedaily.com/rss/health_medicine.xml",
        "https://www.sciencedaily.com/rss/matter_energy/quantum_physics.xml",
        "https://feeds.npr.org/1128/rss.xml",
        "https://www.wired.com/feed/category/science/latest/rss",
        "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
        "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"
    ]
    
    chosen_feed = random.choice(feeds)
    parsed = feedparser.parse(chosen_feed)
    
    if not parsed.entries:
        raise ValueError(f"Nessuna entry trovata nel feed {chosen_feed}")
        
    # Filtriamo quelle già usate
    notizie_nuove = [e for e in parsed.entries if not is_news_used(e.title)]
    
    if not notizie_nuove:
        raise ValueError("Tutte le notizie recenti in questo feed sono già state usate.")
        
    scelta = random.choice(notizie_nuove[:10])
    
    sommario = getattr(scelta, 'summary', '')
    
    return {
        'title': scelta.title,
        'text': sommario,
        'score': "Ultim'ora",
        'source_name': parsed.feed.get('title', 'Feed Salute/Mente')
    }

def fetch_reddit_news() -> dict:
    """Recupera un top post da Reddit a tema mente/scienza/salute/spiritualità."""
    subreddits = [
        "science", "psychology", "neuroscience", "Health", 
        "nutrition", "selfimprovement", "spirituality", 
        "QuantumPhysics", "Meditation", "Awakening", "consciousness"
    ]
    scelta_sub = random.choice(subreddits)
    url = f"https://www.reddit.com/r/{scelta_sub}/top.json?t=week&limit=15"
    
    headers = {
        'User-Agent': 'python:viral_video_generator:v1.0 (by /u/magic_video_bot)'
    }
    
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    data = response.json()
    posts = data['data']['children']
    
    valid_posts = []
    for p in posts:
        post_data = p['data']
        if not post_data.get('is_video', False) and not post_data.get('over_18', False):
            # Controlla se è già stata usata
            if not is_news_used(post_data.get('title', '')):
                valid_posts.append(post_data)
            
    if not valid_posts:
        raise ValueError("Nessun post valido (o non usato) trovato su Reddit")
        
    scelta = random.choice(valid_posts[:5])
    
    return {
        'title': scelta.get('title', ''),
        'text': scelta.get('selftext', ''),
        'score': f"{scelta.get('ups', 0)} upvotes",
        'source_name': f"Reddit (r/{scelta_sub})"
    }

def get_random_viral_news() -> str:
    """
    Seleziona notizie attingendo da fonti a tema benessere, mente e scienza.
    Tiene traccia delle notizie usate.
    """
    scelta_fonte = random.choices(
        ['rss', 'reddit'],
        weights=[40, 60],
        k=1
    )[0]
    
    news_data = None
    
    for _ in range(3): # Prova 3 volte
        try:
            if scelta_fonte == 'rss':
                news_data = fetch_rss_news()
            else:
                news_data = fetch_reddit_news()
            break
        except Exception as e:
            print(f"Errore recupero {scelta_fonte}: {e}. Riprovo alternando fonte...")
            scelta_fonte = 'reddit' if scelta_fonte == 'rss' else 'rss'

    if not news_data:
        fallbacks = [
            {
                'title': "L'abbinamento perfetto: perché dovresti sempre mangiare limone e spinaci insieme",
                'text': "Una nuova ricerca rivela che accoppiare la vitamina C col ferro vegetale sblocca l'assorbimento dei nutrienti in modo incredibile, agendo come un superfood naturale.",
                'score': "Mondiale",
                'source_name': "Science Daily"
            },
            {
                'title': "La geniale abitudine mattutina che accomuna Steve Jobs e Nikola Tesla",
                'text': "Analizzando le routine dei più grandi innovatori, i ricercatori hanno scoperto un pattern comportamentale nelle prime ore del mattino che riprogramma la neuroplasticità per il successo.",
                'score': "Mondiale",
                'source_name': "Psychology Today"
            },
            {
                'title': "Scoperta una connessione fisica tra solitudine e invecchiamento del DNA",
                'text': "I ricercatori hanno trovato la prova biologica che l'isolamento prolungato altera letteralmente il nostro codice genetico.",
                'score': "Mondiale",
                'source_name': "Wired Health"
            },
            {
                'title': "L'effetto Zeigarnik: il vero motivo per cui non riesci a dormire la notte",
                'text': "Una curiosità psicologica svela perché il nostro cervello si rifiuta di 'spegnersi' quando lasciamo compiti a metà durante la giornata.",
                'score': "Mondiale",
                'source_name': "Neuroscience News"
            }
        ]
        news_data = random.choice(fallbacks)
            
    titolo = news_data['title']
    testo = news_data['text']
    score = news_data['score']
    fonte = news_data['source_name']
    
    # Marca come usata per evitare duplicati futuri
    if news_data.get('source_name') != "Science Daily":
        mark_news_used(titolo)
    
    # Pulizia basica
    if testo:
        for tag in ['<p>', '</p>', '<b>', '</b>', '<i>', '</i>', '<br>', '<br/>']:
            testo = testo.replace(tag, '\n' if 'br' in tag or 'p>' in tag else '')
    
    prompt = f"TITOLO DELLA SCOPERTA O NOTIZIA VIRALE (Fonte: {fonte} | Popolarità: {score}):\n{titolo}\n\n"
    if testo and len(testo.strip()) > 20:
        prompt += f"Dettagli aggiuntivi: {testo[:800]}...\n\n"
        
    prompt += "Estrai il vero significato di questa notizia, rendendola una rivelazione inaspettata e profondamente umana. Crea un collegamento originale e logico (sensato) su come questo cambi la vita di chi ascolta (mente, corpo o percezione della realtà). Evita banalità o generalizzazioni, sii specifico e usa un approccio narrativo (storytelling) potente, empatico e altamente virale."
    
    return prompt

if __name__ == "__main__":
    print(get_random_viral_news())
