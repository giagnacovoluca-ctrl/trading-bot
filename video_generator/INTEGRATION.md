# Architettura operativa Video Generator ↔ Conscia-Mente

## Principio fondamentale

`agy` CLI locale è il motore di generazione editoriale. Il sistema non usa API
LLM a pagamento: Python lo invoca tramite `subprocess`, Node tramite `spawnSync`.
Le API esterne rimaste servono solo per media e pubblicazione (Meta Graph,
Pexels/Pollinations e Telegram).

## Flusso promo canonico

1. `video_promo.sh` acquisisce il lock globale `/tmp/video_generator_pipeline.lock`.
2. `agente_tiktok.py --mode promo` genera e valida testo, voce e video.
3. Il video viene pubblicato sui canali configurati.
4. Una copia con nome univoco entra in `conscia-mente/public/videos/`.
5. `scripts/generate-article.mjs` riceve titolo, video ed eventuale eBook.
6. Il generatore restituisce `GENERATED_FILES_JSON`, cioè il manifest dei soli
   file creati.
7. Solo video, articolo e immagini del manifest vengono aggiunti al commit.

Il generatore articoli accetta entrambe le sintassi per compatibilità:

```bash
node scripts/generate-article.mjs "Titolo"
node scripts/generate-article.mjs --topic "Titolo"
```

## Sicurezza dei media Meta

`modules/media_server.py` copia soltanto i media necessari in una directory
temporanea. Il server pubblico non deve mai usare la root del progetto.
Impostare `PUBLIC_MEDIA_HOST` in `.env`; verificare che la porta dinamica sia
raggiungibile da Meta. La directory viene eliminata alla chiusura.

## Variabili richieste

- `PUBLIC_MEDIA_HOST`
- `IG_USER_ID`, `IG_ACCESS_TOKEN`
- `PEXELS_API_KEY` (opzionale)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (opzionali)

## Verifiche senza pubblicazione

```bash
venv_video/bin/python doctor.py
venv_video/bin/python -m unittest discover -s tests -v
venv_video/bin/python -m compileall -q -f . -x '(^|/)(venv_video|chrome_profile)/'
bash -n *.sh
```

Non eseguire gli script `step4_*` durante uno smoke test: pubblicano realmente.
Per produrre un video completo senza upload o modifica del sito usare:

```bash
venv_video/bin/python agente_tiktok.py --mode virale --no-publish --no-site
```

## Concorrenza e retention

I wrapper cron acquisiscono `/tmp/video_generator_pipeline.lock`: se un processo
pesante è già attivo, il nuovo run viene saltato senza sovrascrivere file
condivisi. `maintenance.py` è sempre dry-run salvo `--apply`; non accetta una
retention inferiore a sette giorni.

La history canonica delle nuove generazioni è JSONL in
`used_news_history.jsonl`; il file TXT continua a essere scritto per
compatibilità con i prompt e i cron precedenti.
