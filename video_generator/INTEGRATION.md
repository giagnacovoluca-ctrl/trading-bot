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

## Contratto qualitativo dei copioni

Ogni copione AGY sviluppa un solo concetto e termina con quattro metadati:

```text
FATTO_CENTRALE: affermazione verificabile
TIPO_EVIDENZA: studio, revisione, dato istituzionale o fatto consolidato
LIMITE_EVIDENZA: cosa il dato non dimostra
ANGOLO_NARRATIVO: prospettiva scelta e non ancora usata
```

Prima del rendering, `modules/script_quality.py` controlla fonte, lunghezza,
frasi, CTA, promesse sanitarie assolute e presenza dei metadati. Un controllo
fallito interrompe il job. `used_news_history.jsonl` conserva anche questi
campi, così i prompt successivi possono evitare gli stessi angoli narrativi.

## Sicurezza dei media Meta

`modules/media_server.py` copia soltanto i media necessari in una directory
temporanea. Il server pubblico non deve mai usare la root del progetto.
Impostare `PUBLIC_MEDIA_HOST` in `.env`; verificare che la porta dinamica sia
raggiungibile da Meta. La directory viene eliminata alla chiusura.

Il setup noVNC per il login Instagram ascolta solo su `127.0.0.1`: accedervi
tramite tunnel SSH, senza esporre la porta direttamente su Internet.

## Variabili richieste

- `PUBLIC_MEDIA_HOST`
- `META_GRAPH_VERSION` (default verificato: `v24.0`)
- `IG_USER_ID`, `IG_ACCESS_TOKEN`
- `PEXELS_API_KEY` (opzionale)
- `EMAIL_ENV_FILE` (default: `/home/ubuntu/conscia-mente/.env`), oppure
  `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFICATION_EMAIL` per gli avvisi email

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
retention inferiore a sette giorni. Controlla sia `temp` sia `output` e mostra
lo spazio recuperabile prima di cancellare:

```bash
venv_video/bin/python maintenance.py --days 14 --output-days 30
venv_video/bin/python maintenance.py --days 14 --output-days 30 --apply
```

Il profilo Chrome non viene mai rimosso automaticamente perché contiene la
sessione di pubblicazione.

La history canonica delle nuove generazioni è JSONL in
`used_news_history.jsonl`; il file TXT continua a essere scritto per
compatibilità con i prompt e i cron precedenti.
