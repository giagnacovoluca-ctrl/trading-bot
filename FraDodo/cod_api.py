import aiohttp
import os
import asyncio
from typing import Optional, Dict, Any

COD_API_KEY = os.getenv("COD_API_KEY")

async def get_latest_match(activision_id: str) -> Optional[Dict[str, Any]]:
    """
    Recupera l'ultima partita Warzone per un dato activision_id usando il cookie ACT_SSO_COOKIE
    impostato in .env. Ritorna i dati della partita o None se fallisce o non ha il cookie.
    """
    cookie = os.getenv("ACT_SSO_COOKIE")
    if not cookie:
        raise ValueError("ACT_SSO_COOKIE mancante nel file .env")

    # Formattiamo l'Activision ID, ad esempio User#1234 -> User%231234
    safe_id = activision_id.replace("#", "%23")
    
    # Endpoint generico per le partite Warzone (mw = Modern Warfare / WZ). 
    # platform/uno è la piattaforma "Activision" usata cross-platform.
    url = f"https://my.callofduty.com/api/papi-client/crm/cod/v2/title/mw/platform/uno/gamer/{safe_id}/matches/wz/start/0/end/0/details"

    headers = {
        "Cookie": f"ACT_SSO_COOKIE={cookie}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status != 200:
                    return None
                
                data = await response.json()
                if data.get("status") != "success":
                    return None
                
                matches = data.get("data", {}).get("matches", [])
                if not matches:
                    return None
                
                # Prendi l'ultima partita valida (la prima nella lista)
                last_match = matches[0]
                stats = last_match.get("playerStats", {})
                
                return {
                    "match_id": last_match.get("matchID"),
                    "kills": stats.get("kills", 0),
                    "damage": stats.get("damageDone", 0),
                    "placement": stats.get("teamPlacement", 0)
                }
    except Exception as e:
        print(f"Errore API COD per {activision_id}: {e}")
        return None
