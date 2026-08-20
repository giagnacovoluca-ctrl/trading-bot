#!/bin/bash
export DISPLAY=:0
export PATH=$PATH:/home/ubuntu/.local/bin:/usr/local/bin

cd /home/ubuntu/GIT/video_generator

echo "---" >> cron_agy.log
echo "Avvio CRON: Trend Hunter Autonomo" >> cron_agy.log

# L'agente viene evocato per fare ricerca web sui trend e poi lanciare lo script autonomo
# --dangerously-skip-permissions per permettere all'agente di eseguire comandi sul terminale
agy --dangerously-skip-permissions "Sei il Subagent Trend Hunter Globale. Il tuo scopo è esplorare le notizie di oggi tramite search_web e trovare la scoperta o la notizia PIÙ scioccante in una di queste aree (scegline una casualmente per variare): Neuroscienze, Spazio, Fisica, Storia Segreta, Biologia. Prima di decidere, usa il tool view_file per leggere le ultime righe di 'used_news_history.txt' e ASSICURATI ASSOLUTAMENTE di non scegliere un argomento simile a quelli già usati. Identifica un tema virale nuovo, sintetizzalo in max 3 parole (es. 'Buchi neri supermassicci') e usa il tool run_command per eseguire ESATTAMENTE: 'venv_video/bin/python agente_autonomo.py --topic \"Tuo Argomento\" --mode virale >> cron_agy.log 2>&1'. Non chiedere conferme." >> cron_agy.log 2>&1
