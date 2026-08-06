import sqlite3
import threading
from typing import List, Dict, Any

DB_PATH = 'fradodo.db'

def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    # Players table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS players (
            discord_id TEXT PRIMARY KEY,
            activision_id TEXT NOT NULL,
            points INTEGER DEFAULT 0,
            matches_played INTEGER DEFAULT 0,
            total_kills INTEGER DEFAULT 0,
            total_damage INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0
        )
    ''')
    
    # Matches table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT,
            kills INTEGER,
            damage INTEGER,
            placement INTEGER,
            status TEXT DEFAULT 'approved',
            screenshot_url TEXT,
            ocr_confidence REAL,
            FOREIGN KEY(discord_id) REFERENCES players(discord_id)
        )
    ''')
    
    # Highlights table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS highlights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT,
            message TEXT,
            sent INTEGER DEFAULT 0
        )
    ''')
    
    # Bounties table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bounties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            reward_points INTEGER,
            active INTEGER DEFAULT 1
        )
    ''')
    
    # Insert default bounty if empty
    cursor.execute('SELECT COUNT(*) FROM bounties')
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO bounties (title, description, reward_points) VALUES ('Mostro della Settimana', 'Ottieni più di 30 Kills in una singola partita approvata!', 300)")
    
    try:
        cursor.execute("ALTER TABLE players ADD COLUMN contest_points REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
        
    try:
        cursor.execute("ALTER TABLE matches ADD COLUMN api_match_id TEXT UNIQUE")
    except sqlite3.OperationalError:
        pass
        
    try:
        cursor.execute("ALTER TABLE matches ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    except sqlite3.OperationalError:
        pass
        
    conn.commit()
    conn.close()

def register_player(discord_id: str, activision_id: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO players (discord_id, activision_id) 
        VALUES (?, ?)
    ''', (discord_id, activision_id))
    conn.commit()
    conn.close()

def get_player(identifier: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM players WHERE discord_id = ? OR LOWER(activision_id) = LOWER(?)', (identifier, identifier))
    player = cursor.fetchone()
    conn.close()
    return dict(player) if player else None

def delete_player(discord_id: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM matches WHERE discord_id = ?', (discord_id,))
    cursor.execute('DELETE FROM players WHERE discord_id = ?', (discord_id,))
    conn.commit()
    conn.close()

def match_exists(api_match_id: str) -> bool:
    if not api_match_id:
        return False
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM matches WHERE api_match_id = ?', (api_match_id,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def get_all_players():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM players ORDER BY (points + COALESCE(contest_points, 0)) DESC')
    players = [dict(row) for row in cursor.fetchall()]
    
    # Controlla se hanno partite in sospeso
    for p in players:
        cursor.execute("SELECT COUNT(*) as count FROM matches WHERE discord_id = ? AND status IN ('pending_review', 'pending_review_contest')", (p['discord_id'],))
        p['has_pending'] = cursor.fetchone()['count'] > 0
        
    conn.close()
    return players

def save_match(discord_id: str, kills: int, damage: int, placement: int, status: str = 'approved', screenshot_url: str = None, ocr_confidence: float = 1.0, api_match_id: str = None):
    conn = get_connection()
    cursor = conn.cursor()
    
    if api_match_id:
        # Controlla se la partita è già stata salvata
        cursor.execute('SELECT 1 FROM matches WHERE api_match_id = ?', (api_match_id,))
        if cursor.fetchone():
            conn.close()
            return False

    cursor.execute('''
        INSERT INTO matches (discord_id, kills, damage, placement, status, screenshot_url, ocr_confidence, api_match_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (discord_id, kills, damage, placement, status, screenshot_url, ocr_confidence, api_match_id))
    
    if status == 'approved':
        points_earned = kills * 10 + (50 if placement == 1 else (20 if placement <= 5 else 0))
        win = 1 if placement == 1 else 0
        
        cursor.execute('''
            UPDATE players 
            SET points = points + ?, matches_played = matches_played + 1, 
                total_kills = total_kills + ?, total_damage = total_damage + ?, wins = wins + ?
            WHERE discord_id = ?
        ''', (points_earned, kills, damage, win, discord_id))
        
    conn.commit()
    conn.close()
    return True

def save_contest_match(discord_id: str, kills: int, placement: int, screenshot_url: str):
    conn = get_connection()
    cursor = conn.cursor()
    
    base_points = kills * 10
    multiplier = 1.0
    
    if placement == 1:
        multiplier = 1.8
    elif 2 <= placement <= 5:
        multiplier = 1.6
    elif 6 <= placement <= 10:
        multiplier = 1.4
    elif 11 <= placement <= 21:
        multiplier = 1.2
        
    final_points = base_points * multiplier
    
    # Aggiorna il giocatore aggiungendo i punti contest (e salviamo anche stats generali)
    cursor.execute('''
        UPDATE players 
        SET contest_points = contest_points + ?, total_kills = total_kills + ?, matches_played = matches_played + 1
        WHERE discord_id = ?
    ''', (final_points, kills, discord_id))
    
    # Volendo potremmo salvare nel DB in matches con status 'pending_review_contest' per la revisione manuale
    cursor.execute('''
        INSERT INTO matches (discord_id, kills, damage, placement, status, screenshot_url, ocr_confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (discord_id, kills, 0, placement, 'pending_review_contest', screenshot_url, 1.0))

    conn.commit()
    conn.close()
    return final_points

def get_pending_reviews():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT m.*, p.activision_id 
        FROM matches m 
        JOIN players p ON m.discord_id = p.discord_id
        WHERE m.status IN ('pending_review', 'pending_review_contest')
    ''')
    matches = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return matches

def resolve_review(match_id: int, action: str, new_kills: int = None, new_damage: int = None, new_placement: int = None):
    conn = get_connection()
    cursor = conn.cursor()
    
    if action == 'approve':
        cursor.execute('SELECT * FROM matches WHERE id = ?', (match_id,))
        match = cursor.fetchone()
        if match and match['status'] in ('pending_review', 'pending_review_contest'):
            old_status = match['status']
            k = new_kills if new_kills is not None else match['kills']
            d = new_damage if new_damage is not None else match['damage']
            p = new_placement if new_placement is not None else match['placement']
            
            cursor.execute('UPDATE matches SET status = "approved", kills = ?, damage = ?, placement = ? WHERE id = ?', (k, d, p, match_id))
            
            if old_status == 'pending_review':
                # Era OCR, i punti NON erano stati dati. Diamo i punti base.
                points_earned = k * 10 + (50 if p == 1 else (20 if p <= 5 else 0))
                win = 1 if p == 1 else 0
                cursor.execute('''
                    UPDATE players 
                    SET points = points + ?, matches_played = matches_played + 1, 
                        total_kills = total_kills + ?, total_damage = total_damage + ?, wins = wins + ?
                    WHERE discord_id = ?
                ''', (points_earned, k, d, win, match['discord_id']))
            elif old_status == 'pending_review_contest':
                # I punti (contest) ERANO GIÀ stati dati.
                # Per semplicità, se l'admin ha modificato kills/placement, non calcoliamo la differenza complessa, 
                # approviamo e basta. Il giocatore ha già i punti nella classifica.
                pass
                
    elif action == 'reject':
        cursor.execute('SELECT * FROM matches WHERE id = ?', (match_id,))
        match = cursor.fetchone()
        if match and match['status'] in ('pending_review', 'pending_review_contest'):
            old_status = match['status']
            cursor.execute('UPDATE matches SET status = "rejected" WHERE id = ?', (match_id,))
            
            if old_status == 'pending_review_contest':
                # I punti erano stati dati in anticipo, li dobbiamo SOTTRARRE.
                k = match['kills']
                p = match['placement']
                base_points = k * 10
                multiplier = 1.0
                if p == 1: multiplier = 1.8
                elif 2 <= p <= 5: multiplier = 1.6
                elif 6 <= p <= 10: multiplier = 1.4
                elif 11 <= p <= 21: multiplier = 1.2
                final_points = base_points * multiplier
                
                cursor.execute('''
                    UPDATE players 
                    SET contest_points = contest_points - ?, total_kills = total_kills - ?, matches_played = matches_played - 1
                    WHERE discord_id = ?
                ''', (final_points, k, match['discord_id']))
        
    conn.commit()
    conn.close()

def reset_leaderboard():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE players SET points = 0, matches_played = 0, total_kills = 0, total_damage = 0, wins = 0, contest_points = 0')
    cursor.execute('DELETE FROM matches')
    conn.commit()
    conn.close()

def get_player_matches(discord_id: str, limit: int = 10):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT kills, damage, placement FROM matches WHERE discord_id = ? AND status IN ("approved", "contest") ORDER BY id DESC LIMIT ?', (discord_id, limit))
    matches = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return matches[::-1] # return chronological order

def get_user_match_count(discord_id: str) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM matches WHERE discord_id = ? AND date(created_at, 'localtime') = date('now', 'localtime')", (discord_id,))
    count = cursor.fetchone()['count']
    conn.close()
    return count

def get_active_bounties():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM bounties WHERE active = 1')
    bounties = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return bounties

def insert_highlight(discord_id: str, message: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO highlights (discord_id, message) VALUES (?, ?)', (discord_id, message))
    conn.commit()
    conn.close()

def get_pending_highlights():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT h.id, h.message, p.activision_id 
        FROM highlights h
        JOIN players p ON h.discord_id = p.discord_id
        WHERE h.sent = 0
    ''')
    highlights = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return highlights

def mark_highlight_sent(highlight_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE highlights SET sent = 1 WHERE id = ?', (highlight_id,))
    conn.commit()
    conn.close()
