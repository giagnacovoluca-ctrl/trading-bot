import os
import time
import requests
import subprocess
import re
import threading
import sys

sys.stdout = open("watchdog.log", "a", buffering=1)
sys.stderr = sys.stdout

BOT_URL_LOCAL = "http://localhost:8000/"
CHECK_INTERVAL = 30  
TUNNEL_URL = ""

SERVICES = ["direct", "localhost.run", "serveo.net"]
current_service_index = 0
DIRECT_URL = "https://fradodo.duckdns.org"

tunnel_process = None

def is_bot_alive():
    try:
        response = requests.get(BOT_URL_LOCAL, timeout=5)
        return response.status_code == 200
    except:
        return False

def is_tunnel_alive(url):
    try:
        resp = requests.get(url, timeout=10, verify=False)
        if resp.status_code == 200 and "KSD Contest" in resp.text:
            return True
        return False
    except:
        return False


def kill_bot():
    print("[Watchdog] Sto killando il bot...")
    subprocess.run(["screen", "-S", "fradodo_bot", "-X", "quit"], stderr=subprocess.DEVNULL)

def start_bot():
    print("[Watchdog] Riavvio il Bot...")
    kill_bot()
    os.system("screen -dmS fradodo_bot bash -c 'source venv/bin/activate && ./venv/bin/python main.py'")
    # Diamo 90 secondi al bot per caricare il modello OCR prima di controllarlo
    time.sleep(90)

def _read_tunnel_output(process):
    global TUNNEL_URL
    for line in iter(process.stdout.readline, ''):
        print("[Tunnel]", line.strip())
        match = re.search(r'(https://[a-zA-Z0-9-]+\.(lhr\.life|loca\.lt|serveousercontent\.com))', line)
        if match:
            TUNNEL_URL = match.group(1)
            print(f"[Watchdog] Nuovo URL Tunnel rilevato: {TUNNEL_URL}")
            with open("current_url.txt", "w") as f:
                f.write(TUNNEL_URL)

def start_tunnel():
    global tunnel_process, TUNNEL_URL, current_service_index
    if tunnel_process:
        tunnel_process.terminate()
        tunnel_process = None
    subprocess.run(["pkill", "-f", "serveo.net"], stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-f", "localhost.run"], stderr=subprocess.DEVNULL)
    
    service = SERVICES[current_service_index]
    
    if service == "direct":
        print("[Watchdog] Avvio connessione diretta tramite dominio DuckDNS...")
        TUNNEL_URL = DIRECT_URL
        with open("current_url.txt", "w") as f:
            f.write(TUNNEL_URL)
        print(f"[Watchdog] Nuovo URL Tunnel rilevato: {TUNNEL_URL}")
        # No process to start for direct
        return
        
    print(f"[Watchdog] Avvio il Tunnel tramite {service}...")
    cmd = f"ssh -o ServerAliveInterval=60 -o StrictHostKeyChecking=no -R 80:localhost:8000 {service}"
    tunnel_process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    
    threading.Thread(target=_read_tunnel_output, args=(tunnel_process,), daemon=True).start()
            
def tunnel_monitor():
    global current_service_index
    while True:
        if tunnel_process and tunnel_process.poll() is not None:
            # Il processo è crashato
            print("[Watchdog] Il processo Tunnel è crashato! Passo al servizio di backup...")
            current_service_index = (current_service_index + 1) % len(SERVICES)
            start_tunnel()
            time.sleep(30)
        else:
            time.sleep(5)

# Avvio iniziale
start_tunnel()
time.sleep(5) # dai tempo al tunnel di ottenere l'url

threading.Thread(target=tunnel_monitor, daemon=True).start()

print("🛡️ Watchdog avanzato avviato. Monitoraggio attivo...")
while True:
    bot_ok = is_bot_alive()
    if not bot_ok:
        print("[Watchdog] ⚠️ Bot non risponde! Riavvio in corso...")
        start_bot()
        time.sleep(10)
        
    if TUNNEL_URL:
        tunnel_ok = is_tunnel_alive(TUNNEL_URL)
        if not tunnel_ok:
            print(f"[Watchdog] ⚠️ L'URL {TUNNEL_URL} non risponde o ha perso il dominio! Riavvio e cambio servizio...")
            current_service_index = (current_service_index + 1) % len(SERVICES)
            start_tunnel()
            time.sleep(10)
            
    time.sleep(CHECK_INTERVAL)
