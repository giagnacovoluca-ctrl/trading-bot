from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import os
import easyocr
import re
import database
from ocr_module import reader

app = FastAPI(title="FraDodo Dashboard")

# Monta la cartella static per poter servire le immagini salvate
import os
os.makedirs("web/static/uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="web/static"), name="static")

templates = Jinja2Templates(directory="web/templates")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, error: str = None, success: str = None):
    players = database.get_all_players()
    # Calcola statistiche avanzate
    for p in players:
        mp = p.get('matches_played', 0)
        p['kd_ratio'] = round(p.get('total_kills', 0) / mp, 2) if mp > 0 else 0
        p['avg_damage'] = round(p.get('total_damage', 0) / mp) if mp > 0 else 0
        p['win_rate'] = f"{round((p.get('wins', 0) / mp) * 100)}%" if mp > 0 else "0%"
        
    bounties = database.get_active_bounties()
    return templates.TemplateResponse(request=request, name="index.html", context={"players": players, "bounties": bounties, "error": error, "success": success})

@app.get("/api/player_history/{discord_id}")
async def api_player_history(discord_id: str):
    matches = database.get_player_matches(discord_id, limit=10)
    return {"matches": matches}

@app.post("/register")
async def web_register(request: Request, discord_id: str = Form(...), activision_id: str = Form(...)):
    database.register_player(discord_id, activision_id)
    return RedirectResponse(url="/?success=Giocatore+iscritto+con+successo!", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/upload_screenshot")
async def web_upload(request: Request, discord_id: str = Form(...), screenshot: UploadFile = File(...)):
    player = database.get_player(discord_id)
    if not player:
        return RedirectResponse(url="/?error=Discord+ID+non+trovato.+Iscriviti+prima.", status_code=status.HTTP_303_SEE_OTHER)
        
    img_bytes = await screenshot.read()
    try:
        result = reader.readtext(img_bytes, detail=1) 
        text_lines = [r[1].lower() for r in result]
        confidence_list = [r[2] for r in result]
        avg_confidence = sum(confidence_list) / len(confidence_list) if confidence_list else 0
        
        kills, damage, placement = 0, 0, 0
        
        # Logica di Parsing Spaziale per Tabelle (Completamente Gratuita)
        def get_y_center(bbox): return (bbox[0][1] + bbox[2][1]) / 2
        def get_x_center(bbox): return (bbox[0][0] + bbox[2][0]) / 2

        target_name = player['activision_id'].lower()
        target_y = None

        # 1. Trova la riga (Y) dove si trova il nome del giocatore
        for bbox, text, conf in result:
            if target_name in text.lower() or text.lower() in target_name:
                target_y = get_y_center(bbox)
                break

        if target_y:
            row_items = []
            # 2. Raccogli tutti gli elementi sulla stessa riga (Y simile)
            for bbox, text, conf in result:
                if abs(get_y_center(bbox) - target_y) < 20:
                    row_items.append((get_x_center(bbox), text))
            
            # 3. Ordina da sinistra a destra
            row_items.sort(key=lambda x: x[0])
            
            # 4. Estrai solo i numeri che si trovano A DESTRA del nome
            numbers_after_name = []
            found_name = False
            for x, text in row_items:
                if target_name in text.lower() or text.lower() in target_name:
                    found_name = True
                    continue
                if found_name:
                    # Rimuovi eventuali spazi e controlla se è un numero
                    clean_text = text.replace(" ", "").replace(",", "").replace(".", "")
                    if clean_text.isdigit():
                        numbers_after_name.append(int(clean_text))
            
            # Nelle tabelle di Warzone (Ritorno), i numeri solitamente sono: 
            # [Punteggio, Eliminazioni, Uccisioni, (Assist), (Morti), Danni]
            if len(numbers_after_name) >= 3:
                kills = numbers_after_name[2]
                damage = numbers_after_name[-1]
            elif len(numbers_after_name) == 2:
                # Fallback se ne legge solo due (Kills e Danni)
                kills = numbers_after_name[0]
                damage = numbers_after_name[-1]

        # Estrazione del piazzamento (rimane col regex per l'intestazione generale)
        full_text = " ".join([r[1].lower() for r in result])
        place_match = re.search(r'(?:posizione|placement|piazzamento)[^\d]*(\d+)', full_text)
        if place_match: placement = int(place_match.group(1))

        # Safeguards per evitare che legga i danni come kills in foto ad alta qualità
        if kills > 80 or damage > 40000:
            match_status = "pending_review"
        else:
            match_status = "pending_review" if avg_confidence < 0.6 or (kills == 0 and damage == 0) else "approved"

        # Save locally for admin review if needed
        import shutil
        os.makedirs("web/static/uploads", exist_ok=True)
        safe_filename = screenshot.filename.replace(" ", "_")
        file_path = f"web/static/uploads/{safe_filename}"
        with open(file_path, "wb") as buffer:
            buffer.write(img_bytes)

        database.save_match(
            discord_id=discord_id,
            kills=kills,
            damage=damage,
            placement=placement,
            status=match_status,
            screenshot_url=f"/static/uploads/{safe_filename}",
            ocr_confidence=avg_confidence
        )
        
        if match_status == "approved":
            if kills >= 20 or damage >= 8000:
                database.insert_highlight(discord_id, f"ha droppato {kills} Kills e {damage} danni! Che mostro!")

        if match_status == "approved":
            return RedirectResponse(url="/?success=Screenshot+processato+e+punti+assegnati!", status_code=status.HTTP_303_SEE_OTHER)
        else:
            return RedirectResponse(url="/?success=Screenshot+inviato+in+revisione+(bassa+qualita).", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        return RedirectResponse(url=f"/?error=Errore+OCR:+{str(e)}", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/upload_contest")
async def web_upload_contest(request: Request, player_name: str = Form(...), kills: int = Form(...), posizione: int = Form(...), screenshot: UploadFile = File(...)):
    player = database.get_player(player_name)
    if not player:
        # Registrazione automatica per ospiti
        generic_discord_id = f"web_{player_name.replace(' ', '_')}"
        database.register_player(generic_discord_id, player_name)
        discord_id = generic_discord_id
    else:
        discord_id = player['discord_id']
        
    import shutil
    os.makedirs("web/static/uploads", exist_ok=True)
    safe_filename = screenshot.filename.replace(" ", "_")
    file_path = f"web/static/uploads/{safe_filename}"
    
    img_bytes = await screenshot.read()
    with open(file_path, "wb") as buffer:
        buffer.write(img_bytes)
        
    punti = database.save_contest_match(discord_id, kills, posizione, f"/static/uploads/{safe_filename}")
    
    return RedirectResponse(url=f"/?success=Contest+registrato!+Hai+guadagnato+{punti:.1f}+Punti+Contest.+(Attesa+di+conferma+dallo+Staff)", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/api/delete_player")
async def api_delete_player(request: Request, discord_id: str = Form(...), password: str = Form(...)):
    if password != "dodo2026": # Semplice password hardcoded per l'admin
        return RedirectResponse(url="/?error=Password+errata", status_code=status.HTTP_303_SEE_OTHER)
        
    database.delete_player(discord_id)
    return RedirectResponse(url="/?success=Giocatore+eliminato+con+successo", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    pending = database.get_pending_reviews()
    return templates.TemplateResponse(request=request, name="admin.html", context={"pending": pending})

@app.post("/admin/resolve")
async def admin_resolve(
    request: Request,
    match_id: int = Form(...),
    action: str = Form(...),
    kills: int = Form(None),
    damage: int = Form(None),
    placement: int = Form(None),
    password: str = Form(...)
):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Invalid admin password")
        
    database.resolve_review(match_id, action, kills, damage, placement)
    
    if action == "approve":
        k = kills or 0
        d = damage or 0
        if k >= 20 or d >= 8000:
            cursor = database.get_connection().cursor()
            cursor.execute('SELECT discord_id FROM matches WHERE id = ?', (match_id,))
            res = cursor.fetchone()
            if res:
                database.insert_highlight(res['discord_id'], f"ha droppato {k} Kills e {d} danni in una partita approvata dallo staff!")
                
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/reset")
async def admin_reset(request: Request, password: str = Form(...)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Invalid admin password")
        
    database.reset_leaderboard()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
