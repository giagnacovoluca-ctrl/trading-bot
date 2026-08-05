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

tunnel_process = None

def is_bot_alive():
    try:
        response = requests.get(BOT_URL_LOCAL, timeout=5)
        return response.status_code == 200
    except:
        return False

def is_tunnel_alive(url):
    return True


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
        match = re.search(r'(https://[a-zA-Z0-9-]+\.lhr\.life)', line)
        if match:
            TUNNEL_URL = match.group(1)
            print(f"[Watchdog] Nuovo URL Tunnel rilevato: {TUNNEL_URL}")
            with open("current_url.txt", "w") as f:
                f.write(TUNNEL_URL)

def start_tunnel():
    global tunnel_process, TUNNEL_URL
    if tunnel_process:
        tunnel_process.terminate()
        tunnel_process = None
    subprocess.run(["pkill", "-f", "localtunnel"], stderr=subprocess.DEVNULL)
    
    print("[Watchdog] Avvio il Tunnel tramite localtunnel...")
    cmd = "bash ../start_localtunnel.sh"
    tunnel_process = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    TUNNEL_URL = "https://fancy-rooms-bet.loca.lt"
    print(f"[Watchdog] Nuovo URL Tunnel impostato fisso: {TUNNEL_URL}")
    with open("current_url.txt", "w") as f:
        f.write(TUNNEL_URL)
            
def tunnel_monitor():
    while True:
        if tunnel_process and tunnel_process.poll() is not None:
            # Il processo è crashato
            print("[Watchdog] Il processo Tunnel è crashato!")
            start_tunnel()
            time.sleep(60)
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
            print(f"[Watchdog] ⚠️ L'URL {TUNNEL_URL} non risponde o ha perso il dominio! Riavvio...")
            start_tunnel()
            time.sleep(10)
            
    time.sleep(CHECK_INTERVAL)
