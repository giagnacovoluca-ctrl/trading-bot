import schedule
import time
import subprocess
from rich.console import Console

console = Console()

def avvia_generazione(mode: str):
    console.print(f"[{time.strftime('%H:%M:%S')}] [bold yellow]INIZIO GENERAZIONE VIDEO TIKTOK - MODO: {mode.upper()}[/]")
    try:
        # Avvia l'orchestratore con la modalità corretta
        # Utilizza il python del virtualenv se presente, altrimenti system python
        subprocess.run(
            ["venv_video/bin/python", "agente_tiktok.py", "--mode", mode],
            check=True
        )
        console.print(f"[{time.strftime('%H:%M:%S')}] [bold green]✓ VIDEO {mode.upper()} GENERATO E PUBBLICATO CON SUCCESSO![/]")
    except subprocess.CalledProcessError as e:
        console.print(f"[{time.strftime('%H:%M:%S')}] [bold red]✖ ERRORE NELLA GENERAZIONE DEL VIDEO {mode.upper()}: {e}[/]")

def main():
    console.print("[bold cyan]🤖 BOT GIORNALIERO TIKTOK AVVIATO![/]")
    console.print("Il bot resterà in esecuzione e pubblicherà in automatico:")
    console.print("- Ore 09:00 -> Video Virale (Scoperta live dal web)")
    console.print("- Ore 11:30 -> Video Promo (Promozione ebook)")
    console.print("- Ore 14:00 -> Video Virale (Scoperta live dal web)")
    console.print("- Ore 16:30 -> Video Promo (Promozione ebook)")
    console.print("- Ore 19:00 -> Video Virale (Scoperta live dal web)")
    console.print("- Ore 22:00 -> Video Promo (Promozione ebook)")
    
    # Pianificazione Aggressiva (ma anti-shadowban): 3 Virali e 3 Promo alternati
    schedule.every().day.at("09:00").do(avvia_generazione, mode="virale")
    schedule.every().day.at("11:30").do(avvia_generazione, mode="promo")
    schedule.every().day.at("14:00").do(avvia_generazione, mode="bastian")
    schedule.every().day.at("16:30").do(avvia_generazione, mode="promo")
    schedule.every().day.at("19:00").do(avvia_generazione, mode="virale")
    schedule.every().day.at("22:00").do(avvia_generazione, mode="promo")
    
    # Loop infinito per controllare la programmazione
    while True:
        schedule.run_pending()
        time.sleep(60) # Controlla ogni minuto

if __name__ == "__main__":
    main()
