# Elevator Pitch (30 secondi)

"Ho costruito e gestisco in produzione un sistema di trading algoritmico multi-mercato — memecoin su Solana e Base, futures BTC su Bitget, perpetual su Injective — con esecuzione on-chain reale, dashboard live e un autopilota con motore decisionale rule-based deterministico validato da un risk engine indipendente. È un monorepot Python con orchestrazione multi-thread resiliente, e un layer di self-analytics che ricalibra automaticamente i pesi dei segnali sui trade chiusi tramite Bayesian updating."

---

# Presentazione da 60 secondi

"Negli ultimi mesi ho sviluppato un ecosistema di trading algoritmico composto da diversi sistemi indipendenti che condividono un'infrastruttura comune. Il cuore è un set di scanner che individuano opportunità su memecoin Solana/Base aggregando dati on-chain, Dune Analytics, DexScreener e controlli di sicurezza sui contratti (honeypot, LP lock). Le posizioni vengono gestite da un simulatore con 8 strategie configurabili e, su Base, eseguite realmente on-chain tramite Uniswap/Aerodrome con un oracle di prezzo proprietario.

In parallelo ho un bot trend-following su BTC futures con analisi multi-timeframe e supporti/resistenze dinamici, e — il progetto di cui vado più fiero — un autopilota su Injective Protocol che monitora 29 mercati perpetual con un motore di scoring rule-based deterministico, validato da un risk engine separato che valida ogni decisione (kill switch, sizing, R:R netto). La prima versione di questo motore usava Claude come decision engine: l'ho sostituito con uno scoring esplicito per eliminare latenza e non-determinismo in un loop a 60s, mantenendo però un layer di analytics che ricalcola i pesi dei segnali con Bayesian updating sui trade chiusi. Il sistema include backtest walk-forward con gate per passare da paper a live.

Tutto è orchestrato da processi daemon con auto-restart e alerting, e ho fatto un audit di sicurezza completo per migrare i segreti hardcoded in `.env`."

---

# Presentazione da 3 minuti

"Il mio portfolio è un monorepo che gestisce capitale reale su più mercati, quindi ogni componente ha dovuto affrontare problemi reali di affidabilità, non solo di funzionalità.

**Architettura generale**: il sistema è organizzato in scanner (discovery di opportunità), un simulatore di posizioni (LiveEngine) che applica strategie configurabili, ed executor che eseguono realmente sul mercato. Tutto comunica tramite CSV/JSON con stato persistente, orchestrato da processi daemon multi-thread con backoff esponenziale e watchdog.

**DeFi multi-chain**: ho 5+ scanner che coprono diverse fasi del ciclo di vita di un memecoin — dalla bonding curve di pump.fun alla graduazione su Raydium, fino a token mid/large cap su CEX con pattern di compressione di volatilità. Ogni scanner alimenta un simulatore con strategie di TP/SL/trailing differenziate, validate su backtest reali — per esempio ho calibrato un trailing stop adattivo a 3 fasce basato sul picco di profitto raggiunto, e dei filtri anti-dump/anti-rug basati su backtest con soglia di precisione minima del 60%.

**Esecuzione reale**: su Base ho un executor che fa swap reali tramite Uniswap V3/Aerodrome, con un oracle di prezzo on-chain proprio (necessario perché gli aggregatori standard falliscono su pool a bassa liquidità), gestione di wrap/unwrap WETH e gas reserve.

**Wallet intelligence**: ho costruito un sistema che identifica wallet 'smart money' analizzando i compratori early dei miei trade vincenti, con un ranking che penalizza i bot di sniping, e li monitora in real-time via WebSocket per generare alert di confluenza.

**BTC futures**: un bot trend-following multi-timeframe con rilevamento dinamico di supporti/resistenze, gate di ingresso fee-aware (R:R minimo 4 validato su 91 trade), e circuit breaker su perdite consecutive. Qui ho anche un esempio di debugging importante: ho scoperto che la chiusura manuale delle posizioni era rotta dalla nascita del bot per un bug di 'side' invertito in hedge mode — non era mai emerso perché gli SL/TP nativi dell'exchange chiudevano comunque le posizioni nella maggior parte dei casi.

**Injective Autopilot — il pezzo forte**: un sistema che monitora 29 mercati perpetual, calcola segnali di microstruttura (order book imbalance, CVD divergence, funding z-score, volatility regime) e quando scattano almeno 2 segnali (o 1 segnale 'Tier S' come funding estremo), genera un trigger. Il decision engine calcola uno score di confidenza con una formula esplicita e pesata (conteggio segnali, coerenza voti long/short, intensità z-score, funding, OBI) e ordina i trigger per score, approvando i migliori entro il numero di slot disponibili. Questa decisione viene poi validata da un risk engine completamente separato — kill switch su drawdown, position sizing, R:R netto considerando il funding. Il sistema gira in modalità PAPER con dashboard FastAPI live, e ha un backtest engine walk-forward con criteri oggettivi (PF>1.5, Sharpe>1.5, ≥500 trade) per autorizzare il passaggio a LIVE. Ho anche un layer di analytics che ricalcola i pesi dei segnali con Bayesian updating sui trade chiusi, applicati al ranking dei candidati, in modo che il sistema migliori le proprie decisioni nel tempo senza intervento manuale. La prima versione del decision engine usava Claude via CLI per generare la decisione in JSON: l'ho sostituito con questo scoring deterministico perché in un loop a 60s su 29 mercati la latenza/costo/non-determinismo dell'LLM non si giustificavano, mentre uno scoring esplicito è istantaneo, riproducibile e facilmente debuggabile — ho mantenuto comunque il layer di adaptive learning per la calibrazione continua.

**Prodotto/SaaS**: infine ho costruito un bot Telegram che monetizza i segnali con tier Free/Premium/VIP e pagamenti USDC on-chain verificati automaticamente, completamente isolato (read-only) dal core di trading, con una landing page che si auto-aggiorna e si pubblica su GitHub Pages ogni giorno."

---

# Come Presentare i Progetti

## Injective Autopilot
- **Come introdurlo**: "Il progetto più completo: motore decisionale rule-based deterministico con risk engine separato e layer di adaptive learning — include anche la storia di come ho sostituito un LLM con uno scoring esplicito in produzione."
- **Problema risolto**: sintetizzare segnali quantitativi eterogenei in decisioni di trading riproducibili e veloci, mantenendo i controlli di rischio in un componente separato e indipendente.
- **Perché interessante**: mostra sia capacità di system design quantitativo (formula di scoring, adaptive Bayesian weights) sia maturità ingegneristica nel valutare/sostituire un'integrazione LLM quando non è lo strumento giusto per il contesto (latenza/costo/determinismo).
- **Competenze dimostrate**: async Python, system design, statistica applicata (Bayesian updating, Sharpe, PF), dashboard full-stack, capacità decisionale tecnica (build vs LLM).
- **Risultati da evidenziare**: backtest gate oggettivo per passare a LIVE, layer di adaptive learning Bayesiano, dashboard con 8 viste, formula di scoring trasparente e auditabile.

## DeFi Multi-Chain Scanner & Executor
- **Come introdurlo**: "Il sistema più grande, quello che gestisce realmente l'esecuzione on-chain."
- **Problema risolto**: discovery + execution + exit management su mercati estremamente volatili e con dati spesso inaffidabili.
- **Perché interessante**: copre l'intero stack, dal data engineering all'esecuzione blockchain reale.
- **Competenze dimostrate**: integrazione blockchain multi-chain, gestione stato persistente, ottimizzazione guidata da dati.
- **Risultati da evidenziare**: oracle on-chain proprio, trailing stop adattivo validato su dati, orchestratore con backoff/watchdog.

## Wallet Mirror & Alpha Finder
- **Come introdurlo**: "Un sistema di blockchain intelligence costruito sopra il mio storico di trade."
- **Problema risolto**: identificare e seguire smart money reale, filtrando bot di sniping.
- **Perché interessante**: mostra capacità di reverse engineering API e progettazione di euristiche anti-abuso.
- **Competenze dimostrate**: WebSocket event-driven, data join cross-sistema, rigore nel non promuovere feature non validate.

## BTC Structural Bot
- **Come introdurlo**: come case study di debugging.
- **Quando enfatizzarlo**: se l'intervistatore è un Senior Dev/CTO interessato a debugging e qualità.
- **Quando evitarlo**: se il colloquio è orientato puramente a frontend/cloud — meno rilevante.
- **Cosa enfatizzare**: il bug del bot mai funzionante per la close, scoperto tramite analisi sistematica dei log; la calibrazione fee-aware del R:R minimo.

## Telegram Signal SaaS
- **Come introdurlo**: "L'unico progetto con un modello di business completo end-to-end."
- **Perché interessante**: dimostra product thinking oltre al codice (pricing, tier, marketing automation).
- **Competenze dimostrate**: integrazione pagamenti crypto, automazione deploy, basso accoppiamento architetturale.

## Gem Hunter / Midcap Scanner
- **Quando parlarne**: come esempio di aggregazione multi-API e scoring interpretabile (rule-based, no black box).
- **Quando evitarlo**: se serve tempo limitato, sono i progetti più "secondari" — citarli solo se chiesto esplicitamente di altri sistemi.

---

# Domande Probabili del Recruiter

1. **Come hai iniziato questo progetto e perché?**
   - Breve: per gestione attiva di un capitale crypto personale, poi evoluto in un ecosistema multi-strategia.
   - Approfondita: nato da un bot BTC con previsioni ML inefficaci; ho progressivamente sostituito ML con regole basate su backtest, ed esteso ad altri mercati man mano che individuavo edge validabili.

2. **Qual è il rischio principale di un sistema che esegue trade reali in automatico?**
   - Breve: bug silenziosi che causano perdite prima di essere notati.
   - Approfondita: nel bot BTC un bug nella chiusura posizioni è rimasto invisibile per mesi perché gli SL/TP nativi compensavano; per questo ho aggiunto logging estensivo, alerting su crash, e backtest gate prima di passare a LIVE.

3. **Hai mai usato un LLM come motore decisionale? Perché ora è rule-based?**
   - Breve: sì, la prima versione chiamava Claude via CLI; l'ho sostituito con uno scoring deterministico per latenza, costo e riproducibilità.
   - Approfondita: in un loop a 60s su 29 mercati, una chiamata LLM per trigger introduceva latenza (10-90s), costo per chiamata e non-determinismo (stessa situazione → risposte leggermente diverse), oltre a rischio di JSON malformato. Ho quindi formalizzato la logica in una formula di scoring esplicita (conteggio segnali, coerenza voti, z-score, funding, OBI) — istantanea, riproducibile e facile da debuggare/testare. Ho mantenuto il layer di adaptive learning (Bayesian updating sui pesi dei segnali) per continuare a migliorare il sistema senza reintrodurre un componente non deterministico nel loop critico.

4. **Come gestisci i segreti (chiavi API, chiavi private wallet)?**
   - Breve: tutto in `.env` non versionato, dopo un audit dedicato.
   - Approfondita: ho fatto un audit che ha trovato segreti hardcoded in 8+ file (password SMTP, chiavi CoinGecko/CMC), li ho migrati in `executor/.env` caricato via dotenv prima degli import (per risolvere le variabili a import-time), e creato il `.gitignore` mancante.

5. **Che differenza c'è tra modalità PAPER e LIVE nel tuo sistema?**
   - Breve: PAPER simula fill/equity su DB senza toccare il mercato; LIVE invia ordini reali.
   - Approfondita: in PAPER, `Executor` scrive su DB e passa a `PaperTradingEngine` che simula SL/TP e aggiorna l'equity virtuale; in LIVE chiama `InjectiveClient.create_limit_order`. Il passaggio a LIVE è gated da un backtest con criteri oggettivi (≥500 trade, PF>1.5, Sharpe>1.5, max DD<20%).

6. **Come testi le tue strategie prima di metterle in produzione?**
   - Breve: backtest su dati storici con split walk-forward.
   - Approfondita: per Injective uso uno split 70/30 in-sample/out-of-sample; per filtri DeFi confronto win bloccate vs loss evitate richiedendo precisione >60% prima di attivare un filtro in produzione.

7. **Cosa fa l'orchestratore `run.py`?**
   - Breve: avvia tutti i componenti come thread daemon con auto-restart.
   - Approfondita: backoff esponenziale sui crash (raddoppia, cap 600s, reset dopo uptime sano), alert email dopo 5 crash veloci con throttle 6h, watchdog ogni 5 minuti sui thread critici (solo alert, niente auto-restart per non duplicare thread vivi), refresh schedulato dei wallet alpha ogni 24h.

8. **Come gestisci lo stato tra riavvii?**
   - Breve: JSON/CSV persistenti per posizioni, storico segnali, stato wallet.
   - Approfondita: per esempio `bsr_history.json` persiste lo storico del buy/sell ratio (prima era solo in memoria e si azzerava ad ogni restart, falsando il calcolo del trend); `mirror_state.json` traccia l'ultimo timestamp visto per wallet per rilevare "risvegli".

9. **Qual è la parte più complessa del sistema?**
   - Breve: l'autopilota Injective, per la combinazione di async multi-mercato, formula di scoring e risk management.
   - Approfondita: gestire 29 mercati in parallelo con `asyncio.gather`, calcolare per ciascuno una decisione batch ordinata per score×peso-adattivo entro gli slot disponibili, e mantenere il risk engine completamente disaccoppiato dal motore decisionale.

10. **Hai esperienza con database?**
    - Breve: SQLite/SQLAlchemy async per Injective, JSON/CSV per gli altri sistemi.
    - Approfondita: in Injective uso SQLAlchemy 2.0 async con aiosqlite, modelli per trade/segnali/decisioni/snapshot di margine, con migrazioni runtime via ALTER TABLE per le nuove colonne del layer analytics.

11. **Come gestisci gli errori di rete/API esterne?**
    - Breve: retry con backoff, fallback a fonti alternative.
    - Approfondita: `base_pump_scanner` ritenta con backoff 15s→5min senza terminare il thread se l'RPC è giù all'avvio; `wallet_alpha_finder` pagina le richieste invece di affidarsi al limite di default; `bitget_futures_executor` ha retry ×3 con verifica post-condizione sulla chiusura posizione.

12. **Hai mai fatto code review o lavorato in team su questo progetto?**
    - Breve: progetto solo, ma con disciplina di documentazione (mappa del codebase aggiornata ad ogni sessione).
    - Approfondita: mantengo un `codebase_summary.md` con architettura, firme di funzioni, parametri chiave e log dei bug fix — pratica che riduce il carico cognitivo di ripresa del contesto, utile anche in team.

13. **Come scegli quale exchange/chain usare per ogni strategia?**
    - Breve: in base a liquidità, costi e disponibilità di executor.
    - Approfondita: Solana per scouting memecoin (liquidità alta, costi bassi) ma executor disabilitato per ora; Base per esecuzione reale (oracle on-chain proprio); Bitget per BTC futures (demo+live con stesse chiavi); Injective per perpetual con motore decisionale rule-based.

14. **Cosa succede se il decision engine produce uno score borderline o un trigger "MIXED"?**
    - Breve: viene scartato a monte — i trigger con margine voti long/short ≤1 sono rigettati con score 0.30, sotto la soglia minima di confidenza.
    - Approfondita: `_score()` ritorna subito 0.30 con motivazione `"conflict(L{vl}/S{vs})"` se il margine tra voti long e short è ≤1; il valore è sotto `min_confidence` (0.55 di default) quindi il trigger non entra nemmeno nel ranking. In ogni caso, qualunque score superi la soglia passa comunque dal risk engine prima dell'esecuzione.

15. **Hai pensato alla containerizzazione/deploy?**
    - Breve: non ancora, è un'area di miglioramento.
    - Approfondita: oggi il deploy è single-host con `venv`; containerizzare ogni componente ridurrebbe i conflitti di dipendenze (es. versioni diverse di `web3`/`pyinjective` tra moduli) e faciliterebbe un eventuale deploy multi-macchina.

16. **Come monitori il sistema in produzione?**
    - Breve: dashboard web + alert email.
    - Approfondita: due dashboard FastAPI/Jinja2 (Injective e DeFi) con auto-refresh; alerting email per crash/anomalie con throttle per evitare spam; manca un sistema di metriche centralizzato tipo Prometheus/Grafana.

17. **Quanto tempo impiega il sistema a generare un segnale?**
    - Breve: dipende dallo scanner — da pochi secondi (WebSocket) a un ciclo di 5-8h (midcap).
    - Approfondita: pump_grad/pre_grad reagiscono in tempo reale via WebSocket con polling di fallback ogni 30s; defi_optimized gira a ciclo continuo; midcap_scanner ha un ciclo di 8h data la natura dei mid/large cap.

18. **Come hai validato che i tuoi filtri non eliminano troppe opportunità buone?**
    - Breve: confronto win bloccate vs loss evitate su backtest.
    - Approfondita: regola interna — un filtro entra in produzione solo se la precisione (loss bloccate / totale bloccati) supera il 60%; per midcap con n=28, 9/9 loss sono state bloccate a fronte di 4/19 win sacrificate, tutte su token già in territorio estremo.

19. **Hai un sistema di gestione del rischio complessivo (cross-strategy)?**
    - Breve: per ora ogni sistema ha il proprio risk management; Injective ha il più completo (kill switch, DD limits).
    - Approfondita: ogni "sistema" di trading nel simulatore DeFi ha SL/trailing/cooldown propri; Injective ha kill switch su DD giornaliero/settimanale e margine; manca un risk aggregator globale cross-sistema — area di miglioramento riconosciuta.

20. **Perché hai scelto Python per tutto?**
    - Breve: ecosistema maturo per data/crypto (web3.py, ccxt, pandas) e velocità di iterazione.
    - Approfondita: tutte le librerie chiave (pyinjective, ccxt, web3.py, scikit-learn) sono Python-first; per un sistema solo-developer la coerenza del linguaggio riduce il context-switching, e asyncio è sufficiente per la concorrenza I/O-bound richiesta.

21. **Hai esperienza con WebSocket?**
    - Breve: sì, per monitorare wallet e pool su Solana in tempo reale.
    - Approfondita: ho riscritto `wallet_mirror_bot` da un endpoint premium (`transactionSubscribe`, 403 sul piano attuale) a `logsSubscribe` standard + fetch on-trigger via Enhanced API, con dedup TTL 6h per evitare elaborazioni duplicate.

22. **Come strutturi il codice per evitare duplicazione tra gli executor (Solana/Base/Bitget/...)?**
    - Breve: oggi c'è duplicazione, è un'area di refactoring nota.
    - Approfondita: ogni executor ha peculiarità della propria chain/exchange (oracle, wrap/unwrap, hedge mode) che rendono la condivisione non banale; un'astrazione comune (interfaccia `execute_buy/execute_sell` con strategy pattern per chain) sarebbe il prossimo passo.

23. **Hai esperienza con dashboard/frontend?**
    - Breve: FastAPI + Jinja2 + Plotly, niente SPA.
    - Approfondita: due dashboard real-time con auto-refresh (10s/60s), grafici Plotly per equity curve, pagine di amministrazione con form POST per azioni come il reset del kill switch.

24. **Come gestisci la concorrenza/race condition su file di stato condivisi?**
    - Breve: scritture atomiche e file lock dove serve.
    - Approfondita: `bot_telegram/store.py` fa persistenza JSON atomica; `structural_bot` usa `fcntl.LOCK_EX` per impedire doppie istanze (causa nota di trade duplicati in passato).

25. **Hai automatizzato il deploy di qualcosa?**
    - Breve: sì, la landing page del bot Telegram su GitHub Pages.
    - Approfondita: `landing.py` rigenera l'HTML con statistiche aggiornate e, se configurato un path locale del repo GitHub Pages, fa commit+push automatico ogni 24h tramite `post_recap()`.

26. **Cosa faresti diversamente se ricominciassi da capo?**
    - Breve: userei un DB fin dall'inizio invece di CSV/JSON, e containerizzerei subito.
    - Approfondita: i CSV sono comodi per debug manuale ma rendono difficili query storiche e introducono rischio di race condition; un DB (anche SQLite) con uno schema condiviso tra i sistemi avrebbe semplificato analytics e backtest.

27. **Hai gestito situazioni di perdita/drawdown? Come hai reagito?**
    - Breve: sì, con kill switch e revisione dei parametri via backtest.
    - Approfondita: una sera il kill switch Injective è scattato a DD 6.6% per un bug di enforcement su `max_open_positions`; ho fatto root cause analysis (fetch_positions restituiva sempre vuoto in PAPER), corretto la fonte di verità (`open_trades` in-memory), e aggiunto un endpoint admin per resettare il kill switch in modo controllato.

28. **Qual è il tuo processo per scrivere un nuovo filtro/strategia?**
    - Breve: ipotesi → backtest su dati storici → soglia di precisione → deploy → monitoraggio.
    - Approfondita: per esempio, prima di aggiungere le penalità di score nel midcap scanner ho fatto un backtest su 28 trade (19 win/9 loss), verificato che le nuove regole bloccassero quasi solo le loss, e documentato la soglia (n<30 → da rivalidare in 2-3 settimane).

29. **Come gestisci versioni/dipendenze diverse tra i sotto-progetti?**
    - Breve: `requirements.txt` separati per modulo (root e injective_autopilot).
    - Approfondita: `injective_autopilot` ha un proprio `requirements.txt` con stack diverso (FastAPI, SQLAlchemy, pyinjective) rispetto al resto (scikit-learn, ccxt, web3) — separazione che riflette la futura containerizzazione modulare.

30. **Sei disposto a lavorare con codebase legacy/non tue?**
    - Breve: sì, il mio approccio di documentazione/mappa del codebase nasce proprio per orientarsi rapidamente in sistemi complessi.
    - Approfondita: mantengo `codebase_summary.md` aggiornato ad ogni sessione proprio per ridurre il tempo di "ricostruzione del contesto" — un'abitudine che applico naturalmente anche su codice altrui.

---

# Domande Tecniche Difficili

1. **(CTO) Come garantisci che il decision engine non possa causare un loss illimitato?**
   - Risposta modello: il risk engine è un componente separato e deterministico che valida ogni `TradeDecision` indipendentemente da come è stata generata — controlla R:R netto, position sizing massimo, kill switch su DD giornaliero/settimanale. Il decision engine non ha accesso diretto all'esecuzione: anche uno score anomalo verrebbe bloccato o ridimensionato dal risk engine prima di arrivare all'executor. Questo disaccoppiamento è stato preservato anche quando ho sostituito il decision engine LLM con quello rule-based — il confine di responsabilità non è cambiato.

2. **(Senior Dev) Perché CSV/JSON invece di un database fin dall'inizio?**
   - Risposta modello: per iterazione rapida e debug manuale (un CSV si ispeziona con un editor, un DB no); il trade-off è l'assenza di transazionalità e query storiche complesse. Per Injective, dove serve analytics più ricca, ho usato SQLAlchemy async fin dall'inizio — la scelta è stata pragmatica per modulo, non dogmatica.

3. **(Team Lead) Come eviti che due thread/processi modifichino lo stesso file di stato contemporaneamente?**
   - Risposta modello: per i casi critici uso file lock (`fcntl.LOCK_EX` in structural_bot) e scritture atomiche (write su file temporaneo + rename) in `store.py`. Per i CSV di segnali, ogni scanner scrive il proprio file — non c'è scrittura condivisa concorrente sullo stesso file da processi diversi.

4. **(Security Engineer) Come proteggi le chiavi private dei wallet?**
   - Risposta modello: chiavi in `.env` non versionato, caricate via dotenv solo dal processo che ne ha bisogno (executor); `EXECUTOR_CHAINS` permette di disabilitare l'esecuzione reale per chain non in uso, riducendo la superficie di rischio. Limite riconosciuto: chiavi in chiaro su filesystem locale — in un contesto enterprise userei un secret manager (Vault/KMS) e firma delle transazioni in un ambiente isolato.

5. **(DevOps Engineer) Come gestisci i rollback se un nuovo filtro peggiora le performance?**
   - Risposta modello: ogni modifica ai parametri è documentata con la motivazione e il valore precedente direttamente nel codice/commenti e nel `codebase_summary.md`; un rollback è quindi un revert puntuale del parametro. Non c'è feature flag dinamico — è un'area di miglioramento (es. config esterna ricaricabile a runtime).

6. **(CTO) Quanto è costoso (in termini di latenza/costi) il loop decisionale Injective?**
   - Risposta modello: oggi è sostanzialmente gratuito e a latenza trascurabile — il decision engine è una formula di scoring in-process, calcolata solo per i mercati con trigger attivo (≥2 segnali Tier A/B o 1 Tier S), su un loop a 60s su 29 mercati. Nella prima versione, basata su Claude via CLI, c'era invece un costo per chiamata e una latenza di 10-90s per trigger, con un rate limit dedicato (20 chiamate/h) — uno dei motivi principali per cui sono passato allo scoring deterministico.

7. **(Senior Dev) Come testi codice che dipende da WebSocket/RPC esterni?**
   - Risposta modello: oggi principalmente con DRY_RUN e log estensivi in produzione (gli endpoint blockchain reali sono difficili da mockare fedelmente per comportamenti come 403 su piani premium). Per Injective ho test unitari su segnali/risk engine (`tests/test_signals.py`, `test_risk_engine.py`) che non dipendono da rete. Miglioramento: introdurre fixture/replay di risposte RPC registrate.

8. **(Quant) Come calcoli l'R:R "netto" considerando il funding rate?**
   - Risposta modello: `compute_net_rr_with_funding(entry, sl, tp, direction, hold_hours, funding_rate)` sottrae il costo/guadagno atteso da funding sull'holding period stimato dal R:R lordo; la soglia di accettazione è `net_rr >= min_rr_ratio - 0.01` (tolleranza per errori di floating point), con un pre-check grezzo a `gross_rr >= min_rr_ratio * 0.85` per scartare subito i casi palesemente insufficienti.

9. **(Security Engineer) Nella versione precedente con Claude come decision engine, come gestivi il rischio di un subprocess che resta appeso (hang)?**
   - Risposta modello: era impostato un timeout esplicito (90s) sul subprocess, e `stdin=DEVNULL` preveniva che il processo restasse in attesa di input che non sarebbe mai arrivato (causa nota di hang con CLI interattive). È uno dei rischi operativi (insieme a latenza e costo) che mi ha portato a sostituire quel componente con uno scoring deterministico in-process, che non ha questa classe di problemi.

10. **(CTO) Come scaleresti il sistema da 1 a 10 utenti (es. multi-tenant per il SaaS Telegram)?**
    - Risposta modello: oggi gli abbonati condividono lo stesso flusso di segnali (modello broadcast, non multi-tenant per strategie); per scalare a 10 "utenti" con strategie diverse servirebbe parametrizzare LiveEngine per configurazione utente e isolare lo stato per tenant — il design a CSV per sistema renderebbe questo passaggio non banale, andrebbe migrato a DB con `tenant_id`.

11. **(Senior Dev) Come hai trovato il bug del "side invertito" in hedge mode su Bitget?**
    - Risposta modello: analizzando lo storico log del bot ho notato che ogni tentativo di chiusura manuale falliva con errore 22002 ("No position to close"), ma le posizioni venivano comunque chiuse (dagli SL/TP nativi). Confrontando la documentazione hedge mode di Bitget (close long = buy con tradeSide=close) con il codice, ho trovato che il `side` veniva impostato uguale a quello di apertura invece che invertito/corretto per `tradeSide`.

12. **(Team Lead) Come prevent-i regressioni quando modifichi parametri di trading "magici"?**
    - Risposta modello: ogni soglia ha un commento con il backtest che l'ha generata (n trade, PF, condizioni); prima di un cambiamento rifaccio il backtest con il nuovo valore e confronto. Manca un framework di backtest automatizzato cross-parametro (grid search) — oggi è manuale.

13. **(Quant/Backend) Come hai progettato la formula di scoring per renderla comparabile tra mercati diversi?**
    - Risposta modello: tutti i componenti dello score sono normalizzati o a soglia (z-score, percentuali, conteggi), non prezzi assoluti, quindi sono comparabili cross-market. Lo score base dipende dal numero di segnali attivi (`0.40 + (n-2)*0.10`), poi vengono aggiunti bonus incrementali per intensità (z-score≥2.5/3.0, funding z-score≥3, OBI≥0.90) e per coerenza dei voti long/short, con un cap a 0.95 e un rigetto immediato (score 0.30) se i segnali sono contraddittori (margine voti ≤1).

14. **(DevOps) Come distingui un crash "vero" da un riavvio volontario nell'orchestratore?**
    - Risposta modello: il backoff esponenziale si resetta dopo un periodo di "uptime sano"; un riavvio manuale (kill + restart del processo padre) non interagisce con questa logica perché è interna al processo `run.py` — un riavvio volontario richiede di fermare l'intero `run.py`, non i singoli thread.

15. **(Security Engineer) Come verifichi che un pagamento USDC sia effettivamente arrivato e non sia un replay/duplicato?**
    - Risposta modello: ogni invoice ha un importo univoco a livello di centesimi (es. 49.74 vs 49.81 per richieste diverse), quindi il watcher on-chain associa il transfer all'invoice tramite match esatto sull'importo all'interno di una finestra temporale; un duplicato con lo stesso importo dallo stesso wallet andrebbe gestito con un controllo aggiuntivo sul transaction hash già processato (verificare se implementato — punto da approfondire onestamente in colloquio).

16. **(Quant) Cosa significa che l'adaptive scorer è "neutrale sotto 10 attivazioni"?**
    - Risposta modello: per ogni segnale, finché non si sono osservate almeno 10 attivazioni (trade in cui quel segnale era attivo), il peso resta al valore neutro (1.0) — evita di assegnare pesi estremi basati su pochissimi campioni, un classico problema di varianza alta con n piccolo nel Bayesian updating.

17. **(CTO) Hai considerato i costi di esecuzione (gas, slippage, fee) nei tuoi backtest?**
    - Risposta modello: sì, esplicitamente per il bot BTC (fee taker 0.12% round-trip incluse nel calcolo che ha portato a `MIN_DYNAMIC_RR=4.0`); per Base, l'executor gestisce gas reserve (0.001 ETH) e min_out adattivo per tipo di exit, ma il backtest dei filtri DeFi è principalmente su prezzo, non include sistematicamente slippage/gas — area da rafforzare.

18. **(Senior Dev) Come gestisci versioni diverse di una stessa libreria tra moduli (es. web3.py)?**
    - Risposta modello: ogni macro-modulo ha il proprio `requirements.txt`/venv quando le dipendenze divergono significativamente (es. `injective_autopilot` ha uno stack a parte); per il resto condivido un venv unico, accettando il rischio di conflitti minori dato che sono moduli dello stesso processo logico.

19. **(DevOps) Come faresti monitoring "vero" (Prometheus/Grafana) su questo sistema?**
    - Risposta modello: esporrei metriche custom (numero posizioni aperte, equity, segnali/ora, errori per componente) via un endpoint `/metrics` in formato Prometheus dalle dashboard FastAPI già esistenti, e configurerei Grafana per dashboard storiche e alerting basato su soglie invece che sulla sola email.

20. **(AI Engineer) Avendo già lavorato con un decision engine basato su LLM, in che scenari lo riproporresti rispetto a uno rule-based?**
    - Risposta modello: lo riproporrei dove il valore aggiunto è il *reasoning su contesto qualitativo/non strutturato* (es. notizie, sentiment, contesto macro non riducibile a poche feature numeriche) e dove la cadenza decisionale è bassa (minuti/ore, non secondi), così la latenza diventa accettabile. Per un loop quantitativo ad alta frequenza su segnali numerici già ben definiti, come il sentinel a 60s su 29 mercati, un motore rule-based esplicito è preferibile per costo, latenza, determinismo e testabilità — l'LLM resterebbe utile come livello di analisi offline (es. spiegare/raggruppare pattern nei post-mortem) più che nel loop decisionale real-time.

---

# Punti di Forza del Portfolio

- **Sistema di produzione reale**, non demo: gestisce capitale reale, esecuzione on-chain, dati live.
- **Maturità ingegneristica nella scelta degli strumenti**: aver prototipato un decision engine con LLM e poi sostituirlo con uno scoring deterministico per ragioni di latenza/costo/affidabilità è un segnale forte di pensiero critico, non di "AI per moda".
- **Disciplina di validazione statistica** (backtest, soglie di precisione, walk-forward, adaptive Bayesian weights) prima di ogni modifica in produzione.
- **Debugging documentato di bug critici** con root cause analysis chiara — ottimo materiale per colloqui tecnici.
- **Ampiezza tecnologica**: blockchain (Solana + EVM + Cosmos/Injective), exchange tradizionali, motori decisionali quantitativi/adaptive, dashboard, SaaS/billing.
- **Pratiche di sicurezza proattive** (audit segreti, gitignore, separazione ambienti).
- **Documentazione viva del codebase** — mostra maturità organizzativa rara in progetti solo-developer.

# Punti Deboli del Portfolio

- **Assenza di test automatizzati estesi** (solo Injective ha una suite di test minimale).
- **Nessuna containerizzazione/CI/CD** — tutto deploy manuale single-host.
- **Persistenza basata su CSV/JSON** per la maggior parte dei sistemi — non scalabile/concorrente.
- **Monitoring limitato** a dashboard locali + email, nessuna metrica centralizzata.
- **Progetto solo-developer**: nessuna esperienza diretta di code review/collaborazione documentabile da questo portfolio (da compensare con esempi da altre esperienze, se presenti).
- **Dominio "trading crypto"** può essere percepito come rischioso/speculativo da alcuni recruiter — va inquadrato come "contesto applicativo" per competenze trasferibili (sistemi real-time, automazione, decision engine quantitativi).

# Strategia di Presentazione

**Ordine dal più al meno impressionante:**

1. **Injective Autopilot** — apri con questo. È il progetto con la maggiore densità di competenze moderne (decision engine quantitativo, async, risk management, dashboard, learning loop adaptive) ed è quello che genera più domande interessanti — incluso lo storytelling sulla sostituzione dell'LLM con uno scoring deterministico.
2. **DeFi Multi-Chain Scanner & Executor** — secondo, mostra ampiezza (blockchain reale, multi-chain, orchestrazione).
3. **Wallet Mirror & Alpha Finder** — terzo, ottimo se l'azienda ha interesse in blockchain analytics/cybersecurity on-chain.
4. **Telegram Signal SaaS** — quarto, da usare se il ruolo ha componente "prodotto" o business.
5. **BTC Structural Bot** — quinto, tienilo pronto come case study di debugging se emerge il tema "qualità del codice/bug critici".
6. **Gem Hunter / Midcap Scanner / Orchestrazione** — menzionali solo se chiesto di altri sistemi o per rispondere a "cos'altro c'è nel monorepo".

Per ciascuno, segui lo schema: **problema → architettura in 2 frasi → 1 sfida tecnica → 1 risultato misurabile**. Evita di scendere nei dettagli di implementazione a meno che non venga chiesto esplicitamente — tieni pronti i dettagli ma apri sempre con il "perché".

---

# ANALISI FINALE — Classifica dei 10 Migliori "Progetti"

| # | Progetto | Complessità Tecnica | Valore Commerciale | Innovazione | Valore Recruiter | Junior | Mid-level | Senior |
|---|----------|---------------------|---------------------|-------------|-------------------|--------|-----------|--------|
| 1 | Injective Autopilot (rule-based + adaptive learning) | 8 | 7 | 8 | 9 | Basso | Alto | Alto |
| 2 | DeFi Multi-Chain Scanner & Executor | 8 | 8 | 7 | 8 | Medio | Alto | Alto |
| 3 | Wallet Mirror & Alpha Finder | 7 | 7 | 8 | 8 | Basso | Alto | Medio-Alto |
| 4 | Telegram Signal SaaS | 6 | 8 | 6 | 7 | Medio | Alto | Medio |
| 5 | BTC Structural Bot | 7 | 6 | 5 | 7 | Medio | Alto | Medio |
| 6 | Orchestrazione/Infrastruttura (run.py) | 6 | 6 | 5 | 7 | Medio | Alto | Medio |
| 7 | Gem Hunter (gemmeV3) | 6 | 5 | 6 | 6 | Medio | Medio | Medio |
| 8 | Midcap Scanner (BB Squeeze) | 6 | 5 | 5 | 6 | Medio | Medio | Medio |
| 9 | Backtest Engine (Injective) | 5 | 5 | 6 | 6 | Basso | Medio | Medio-Alto |
| 10 | Pump/PreGrad Monitor (pump.fun) | 5 | 4 | 6 | 5 | Medio | Medio | Basso-Medio |

## 1. I 3 progetti da mostrare assolutamente
1. **Injective Autopilot** — densità tecnica più alta, decision engine quantitativo + adaptive learning, storytelling forte (incluso il "downgrade" consapevole da LLM a rule-based).
2. **DeFi Multi-Chain Scanner & Executor** — ampiezza (blockchain reale, multi-chain, dati live).
3. **Wallet Mirror & Alpha Finder** *oppure* **Telegram Signal SaaS**, a seconda che il colloquio sia più tecnico/blockchain (mirror) o più "prodotto/business" (SaaS).

## 2. Progetti che possono essere omessi
- **Pump/PreGrad Monitor**: troppo di nicchia, rischia di spostare la conversazione su "trading di memecoin" più che su competenze trasferibili — citarlo solo se chiesto di dettagli sugli scanner.
- **Gem Hunter v2** (esplicitamente non attivo) — non menzionarlo, è codice legacy conservato.
- **Midcap Scanner**: utile come dettaglio ma ridondante rispetto a DeFi Scanner se il tempo è limitato.

## 3. Progetti che avvicinano a un ruolo specifico

- **Software Developer**: DeFi Multi-Chain Scanner & Executor (architettura modulare, multi-linguaggio di integrazione, gestione stato).
- **Automation Engineer**: Orchestrazione/Infrastruttura (run.py) + Telegram Signal SaaS (pipeline end-to-end automatizzate).
- **DevOps Engineer**: Orchestrazione/Infrastruttura — backoff, watchdog, alerting, gestione segreti (ma evidenziare onestamente la mancanza di containerizzazione/CI come "prossimo passo" pianificato).
- **Cybersecurity Analyst**: Audit segreti (trasversale) + Wallet Mirror & Alpha Finder (analisi comportamentale on-chain, anti-spam/anti-bot) + GoPlus security checks in Gem Hunter.
- **AI Engineer**: Injective Autopilot — l'esperienza pratica di prototipazione con Claude come decision engine (subprocess, prompt JSON, parsing) e la successiva sostituzione con uno scoring rule-based è un buon talking point su "quando usare un LLM e quando no"; va però presentata come esperienza passata, non come componente attuale del sistema.
- **Quant Developer**: Injective Autopilot (decision engine a scoring esplicito, adaptive scorer Bayesiano, backtest, R:R netto, Sharpe/PF) + BTC Structural Bot (calibrazione fee-aware, S/R multi-timeframe) + Midcap Scanner (scoring e validazione statistica) — l'asse più forte in assoluto.
- **System Administrator**: Orchestrazione/Infrastruttura (gestione processi daemon, lock file, log rotation) — il più debole come fit puro, ma utilizzabile per dimostrare competenze Linux di base.
