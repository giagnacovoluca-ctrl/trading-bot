# Memoria operativa: ecosistema ConsciaMente

Ultimo aggiornamento verificato: **5 settembre 2026**.

Questa è la memoria di lungo periodo per il collegamento fra generatore video,
Instagram, TikTok e sito Next.js. Prima di modificare cron, CTA, tracking, lead
generation o pubblicazione, leggere questo file insieme ad `AGENT_GUIDE.md`,
`INTEGRATION.md` e `/home/ubuntu/conscia-mente/AGENTS.md`.

## Obiettivo

La catena da preservare è: contenuto social coerente → CTA eseguibile → pagina
ConsciaMente → attribuzione → azione misurabile → lead affidabile → proposta
commerciale pertinente → metriche usate per migliorare i contenuti successivi.

## Architettura

- Video generator: `/home/ubuntu/GIT/video_generator`
- Sito Next.js: `/home/ubuntu/conscia-mente`
- Produzione: `https://conscia-mente.vercel.app`
- Hub link proprietario: `https://conscia-mente.vercel.app/links`
- Registro TikTok: `output/upload_log.json`
- Snapshot TikTok: `scripts/analytics.json`
- Cron: crontab dell'utente `ubuntu`

Il sito usa Vercel e il push su `main` avvia il deploy. Il generatore lavora sul
VPS. Il filesystem di una funzione Vercel non è un database persistente.

## Pianificazione verificata

Dal 05/09 è attiva la campagna qualità descritta in
`crontab_quality_20260905.txt`, confrontata con il crontab effettivo:

- TikTok: video promo quotidiano alle 17:30; carosello promo alle 11:30 di
  martedì, giovedì e sabato. Video generalisti e Bastian sospesi per la campagna.
- Instagram: Reel aesthetic quotidiano alle 10:00, Storia alle 09:30 e
  pubblicazioni condivise dalle pipeline; Numerologia/Oracolo domenica alle 15:30.
- Sito: Giornalista Fantasma martedì e venerdì alle 10:15; generazione corsi
  sospesa; newsletter sabato alle 10:00. Gli articoli associati ai video promo
  restano una produzione aggiuntiva: due è la frequenza degli articoli SEO,
  non un limite assoluto di tutti gli articoli.
- Analisi alle 23:00 sotto il lock della pipeline, lead ogni ora al minuto 05,
  report domenica alle 22:30. Conservato il cron di manutenzione Vercel.

Backup precedente: `/home/ubuntu/project_backups/conscia-cron-before-20260905.txt`.

Le pipeline video condividono `/tmp/video_generator_pipeline.lock`; non
rimuovere il lock senza una protezione equivalente.

## CTA ufficiali

Non promettere “commenta e ti mando il link nei DM” finché non esiste e non è
verificata un'automazione ManyChat equivalente.

- PDF via email (Acqua, Epigenetica, Nervo Vago): “Ricevi il PDF gratuito dal link in bio.”
- Anteprima online (Meditazione, Cibo, Integratori): “Leggi gratuitamente l'anteprima dal link in bio.”
- TikTok usa le equivalenti CTA brevi definite nel catalogo canonico.
- Numerologia/Oracolo: “Prova gratis dal link in bio.”
- Contenuto virale: domanda pertinente per commenti, senza vendita forzata.

Destinazioni corte raccomandate per le bio (landing dedicate, URL visibile corto):

- Instagram: `https://conscia-mente.vercel.app/ig`
- TikTok: `https://conscia-mente.vercel.app/tt`

`src/lib/analytics.js` riconosce `/ig` e `/tt` e assegna internamente gli stessi
valori UTM senza mostrare una query lunga all'utente.

Dal 01/09/2026 `/ig` e `/tt` non sono più rewrite dell'hub generale `/links`:
sono pagine statiche dedicate alla conversione social. Mostrano subito le sei
risorse gratuite, senza header globale, Oracolo flottante, exit popup o offerte
Amazon concorrenti. Ogni selezione aggiunge un `utm_content` del tipo
`profile_<risorsa>`, che misura la risorsa scelta senza fingere di conoscere il
singolo post di provenienza. Il banner cookie usa la variante compatta anche su
queste due pagine.

La modifica delle bio è esterna e va verificata sul profilo; il codice non la
esegue automaticamente.

## Catalogo ebook canonico

Titoli, file sorgente, ASIN, tipo di consegna, destinazioni, CTA e pesi
editoriali vivono in `/home/ubuntu/conscia-mente/src/data/ebooks.json`.
Il generatore li legge tramite `modules/ebook_catalog.py`. Le modalità promo
devono scegliere prima l'ebook e poi un concetto pertinente; la modalità virale
può continuare a usare la rotazione editoriale generalista.

## Attribuzione e conversioni

`src/lib/analytics.js` conserva in `localStorage` UTM, landing iniziale e
referrer. Gli eventi inviati a GA4 e, con consenso marketing, Meta Pixel sono:

- `social_landing_view`, `social_link_click`, `lead`
- `test_complete`, `numerology_complete`, `oracle_start`
- `outbound_click` (Amazon/Corsi.it e altre uscite commerciali)

I form passano l'attribuzione a `/api/subscribe`. La notifica amministrativa
del lead contiene la sorgente. Per test, Oracolo e Numerologia l'API non
restituisce successo se la registrazione via email fallisce.

La pagina `/links` usa `TrackedLink`. Header e footer puntano all'hub interno,
non più a Direct.me. La CTA sticky degli articoli misura la selezione della
risorsa gratuita; il clic Amazon viene misurato separatamente dopo la fruizione.

## Lead e persistenza

In sviluppo i lead entrano in `subscribers.txt` come:

`email - interesse - sorgente`

In produzione la copia durevole garantita è la notifica Gmail amministrativa,
perché Vercel non garantisce persistenza su file. Le risorse gratuite inviano
anche una mail all'utente, lasciando traccia nella posta inviata. Non usare mai
un file Vercel come unica fonte di verità.

Evoluzione raccomandata quando esistono credenziali dedicate: Supabase, Brevo o
MailerLite con consenso e cancellazione. Non inventare chiavi e non aggiungere
servizi esterni senza autorizzazione.

Dal 30/08/2026 la posta amministrativa viene sincronizzata ogni ora da
`/home/ubuntu/conscia-mente/scripts/lead_automation.py` nell'archivio privato
`/home/ubuntu/vps_services/conscia_leads/leads.jsonl` (permessi `0600`). Il
parser accetta soltanto notifiche strutturate con email e risorsa valide. Le
nuove form salvano separatamente `Consenso marketing: SI/NO`: i follow-up a 1
e 3 giorni partono esclusivamente per `SI`, mentre i lead precedenti restano
archiviati senza marketing. Ogni invio ha una chiave idempotente in
`followup_state.json`, per evitare doppioni anche se il cron gira ogni ora.

La domenica alle 22:30 `funnel_report.py` invia via Gmail il riepilogo degli
ultimi sette giorni (lead, consenso, risorsa e sorgente UTM). Non confondere
questo archivio con GA4/Meta: visite e clic restano nei rispettivi pannelli.

## Landing di conversione

La pagina `/links` offre tre percorsi brevi:

- `/inizia/stress` → guida sul nervo vago;
- `/inizia/energia` → manuale sull'acqua;
- `/inizia/identita` → numerologia.

Le landing hanno evento `view_content`, CTA tracciate e sono `noindex` perché
pensate per traffico social/campagne, non per competere con gli articoli SEO.

## Feedback loop TikTok

Tutte le nuove pubblicazioni TikTok devono avere `platform: "tiktok"` nel log:

- `agente_tiktok.py` registra video virali/promo;
- `agente_carosello.py` registra caroselli virali/promo/Bastian;
- i Reel solo Instagram non entrano nel registro TikTok.

Alle 23:00 `analizza_risultati.py` legge le view, converte formati come `842`,
`1.2K` e `3,5M`, poi chiama `update_tiktok_metrics()`. L'associazione usa
esclusivamente l'ID del post: l'ordine della griglia non è affidabile con post
fissati o rimossi. I percorsi sono assoluti rispetto allo script anche dal cron.
L'uploader tenta di salvare l'ID dalla risposta di pubblicazione. Le entry senza
ID restano prive di metriche; un esito ambiguo non attiva un secondo upload.
Uno snapshot senza metriche è marcato `no_metrics`, non zero visualizzazioni.
`get_topic_weights()` usa i dati disponibili, inclusi gli insight Instagram.

Dal 30/08/2026 `modules/content_tracking.py` crea per ogni upload TikTok/Reel un
ID del tipo `stress-tt-a1b2c3d4` e registra internamente ID, piattaforma, focus
e URL. Non inserire URL nelle caption: Instagram e TikTok non li rendono
normalmente cliccabili. Il traffico pubblico usa il link in bio; il redirect
`/c/<id>` resta disponibile per superfici realmente cliccabili. Quando un lead
contiene lo stesso `utm_content`, il peso della categoria aumenta con un limite
massimo, così le conversioni influenzano la rotazione senza monopolizzarla.

Il link in bio identifica la piattaforma, non il singolo post. Non dichiarare
una precisione che le piattaforme non offrono.

Dal 01/09/2026 i nuovi ID usano, quando il testo lo permette, la risorsa esatta
(`meditazione`, `cibo`, `integratori`, `acqua`, `epigenetica`, `nervo-vago`):
un eventuale `/c/<id>` cliccabile porta direttamente alla landing canonica
dell'anteprima o del PDF. Gli ID storici `stress`, `energia`, `identita` e
`risorse` restano supportati. Il normale link in bio continua invece ad avere
attribuzione onesta a livello di piattaforma e risorsa selezionata.

`analizza_risultati.py` usa per impostazione predefinita `chrome_profile`, lo
stesso profilo TikTok persistente e autenticato della pubblicazione; solo in sua
assenza ripiega su browser temporaneo e `cookies.txt`. Il cron delle 23:00 non
richiede nuovi argomenti. Evitare di eseguire l'analisi mentre è attivo un
uploader che usa lo stesso profilo.

## CTA video e durata promo

Dal 01/09/2026 la card finale dei video promo non contiene più testo Amazon
hard-coded. `agente_tiktok.py` legge dal catalogo canonico il tipo di consegna e
passa a `step3_sottotitoli.py` una delle due promesse corrette:

- `ANTEPRIMA GRATUITA` + `SENZA REGISTRAZIONE` per Meditazione, Cibo e
  Integratori;
- `PDF GRATUITO VIA EMAIL` per Acqua, Epigenetica e Nervo Vago.

La destinazione resta `APRI IL LINK IN BIO`. La card dura gli ultimi sette
secondi e adatta automaticamente il font alla larghezza. I copioni promo sono
richiesti in 75-90 parole, con obiettivo 25-35 secondi. Anche i Reel aesthetic
mostrano a schermo `ANTEPRIMA GRATUITA · LINK IN BIO` oppure
`PDF GRATUITO · LINK IN BIO`, oltre alla CTA coerente in caption.

`agente_story.py` genera una storia editoriale strutturata (hook da 4-9 parole,
insight da 18-38, azione da 7-18), la rigenera fino a tre volte se non rispetta
i limiti e blocca alcune affermazioni fisiologiche categoriche. Il layout usa
la CTA del catalogo: “PDF gratuito” per le tre risorse via email e “Anteprima
gratuita” per le tre letture online. L'API attualmente usata pubblica
l'immagine ma non crea automaticamente uno sticker link: non simulare uno
sticker non cliccabile.

## Email operative

Telegram è stato sostituito da `modules/email_notifications.py`, che riusa
`/home/ubuntu/conscia-mente/.env` e le variabili `GMAIL_USER`,
`GMAIL_APP_PASSWORD`, `NOTIFICATION_EMAIL`. Un errore SMTP non deve bloccare il
cron. Invio SMTP reale verificato il 30/08/2026.

## Sicurezza operativa

- Score video minimo 7/10 dopo retry; sotto soglia non pubblicare.
- Un 5/10 non deve chiudere il calendario: `agente_tiktok.py` termina con codice
  temporaneo `75` e `run_agent_until_publish.sh` riparte con una nuova selezione
  editoriale, fino a 5 argomenti (configurabile, massimo 8). Solo gli errori
  tecnici reali fermano subito il job. Esauriti i tentativi, non abbassa la
  soglia: invia email e lascia il recupero al cron successivo.
- Una risposta `Validazione fallita` del servizio AGY non vale 0/5: usa codice
  temporaneo `74`, viene ritentata dal wrapper e non viene confusa con un
  giudizio editoriale. Il log conserva anche la motivazione estratta (`SCORE`,
  `MOTIVAZIONE`, `PROBLEMI`) per capire se il difetto è del copione o del
  valutatore.
- Il preflight qualità/sicurezza viene eseguito subito dopo `rag_generator.py` e
  prima di scaricare o generare immagini. Un contenuto respinto non consuma più
  tre chiamate Pollinations inutili; il controllo completo resta dopo la
  normalizzazione come seconda barriera.
- Non confondere blocco qualità ed errore upload.
- Pexels 401/403 attiva il fallback; Pollinations mantiene fallback locale.
- Non eseguire uploader social nei test: usare `--no-publish` e `--no-site`.
- Non committare log, cookie, profili Chrome, token o asset temporanei.
- `/home/ubuntu/GIT` può essere molto sporco: aggiungere solo file verificati.

## Verifiche standard

```bash
cd /home/ubuntu/conscia-mente
npm test
npm run build

cd /home/ubuntu/GIT/video_generator
venv_video/bin/python -m unittest discover -s tests -v
venv_video/bin/python -m py_compile agente_tiktok.py agente_carosello.py \
  agente_cosciamente.py agente_story.py analizza_risultati.py \
  modules/feedback_loop.py modules/email_notifications.py
bash -n video_virale.sh video_promo.sh cron_cosciamente.sh start_story_cron.sh
```

Prima di dichiarare una pubblicazione riuscita, cercare l'ID/post o la frase di
successo nel log. La sola presenza dell'MP4 non prova l'upload.

## Completamento qualità e conversione — 05/09/2026

- Landing PDF, anteprime e pagine `/libri/[id]` condividono un layout compatto
  senza header globale, Oracolo flottante o exit popup. Banner cookie compatto.
- Tre nuove guide `Guida_*_2026.pdf`, con anteprime e fonti, sono collegate dal
  catalogo; vecchi PDF e manoscritti conservati. `scripts/render-guides.py` nel
  sito consente di rigenerarle da `src/data/guides.json`.
- Le CTA indicano il nome della risorsa da scegliere. Le pipeline promo passano
  `CONSCIA_RESOURCE_ID` per evitare di indovinare la destinazione dal testo.
- Nuovi articoli: verifica del testo effettivo delle fonti prima di creare media
  e pubblicare; bloccati claim non sostenuti. I controlli automatici non sono una
  revisione medica. Pagina `/metodo-editoriale` e due articoli corretti.
- Newsletter e follow-up usano l'archivio VPS, ultimo consenso e disiscrizioni
  con oggetto STOP; follow-up configurati per i sei libri. I test di invio usano
  simulazioni, non email reali.
- Tracciamento: scadenza attribuzione a 30 giorni, reset alla nuova sorgente,
  visite differite fino al consenso e fine anteprima distinta dal clic Amazon.
- I voti qualità fissi dei caroselli sono stati rimossi. Nessuna metrica o
  vendita viene inventata quando manca la misurazione.

## Limiti e prossimi passi

1. Aggiornare le bio social verso gli URL UTM proprietari sopra.
2. Configurare un database/CRM quando saranno disponibili account e chiavi.
3. Aggiungere automazione DM solo dopo un test end-to-end reale.
4. Aggiungere like/commenti/condivisioni quando TikTok li espone stabilmente.
5. Verificare gli eventi in GA4 DebugView e Meta Events Manager.

## Stato al termine dell'integrazione

- CTA senza promesse DM false: implementate nei generatori attivi.
- Hub `/links`: collegato da header e footer.
- Landing `/ig` e `/tt`: dedicate, compatte e orientate alla risorsa promessa.
- UTM, attribuzione lead ed eventi GA4/Meta: implementati.
- Clic Amazon sticky: tracciati.
- Feedback TikTok futuro: collegato alle view reali per entry esplicite.
- Campagne contenuto: ID breve collegato a landing, lead e feedback editoriale.
- Notifiche operative: email, non Telegram.
