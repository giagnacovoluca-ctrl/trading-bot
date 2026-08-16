import sqlite3

def migrate():
    conn = sqlite3.connect('fradodo.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matches_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT,
            kills INTEGER,
            damage INTEGER,
            placement INTEGER,
            status TEXT DEFAULT 'approved',
            screenshot_url TEXT,
            ocr_confidence REAL,
            api_match_id TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(discord_id) REFERENCES players(discord_id)
        )
    ''')
    
    try:
        cursor.execute('''
            INSERT INTO matches_new (id, discord_id, kills, damage, placement, status, screenshot_url, ocr_confidence, api_match_id)
            SELECT id, discord_id, kills, damage, placement, status, screenshot_url, ocr_confidence, api_match_id FROM matches
        ''')
    except sqlite3.OperationalError as e:
        if 'no such column' in str(e) and 'api_match_id' in str(e):
             # Try without api_match_id if it doesn't exist
             cursor.execute('''
                INSERT INTO matches_new (id, discord_id, kills, damage, placement, status, screenshot_url, ocr_confidence)
                SELECT id, discord_id, kills, damage, placement, status, screenshot_url, ocr_confidence FROM matches
             ''')
        else:
             raise e

    cursor.execute('DROP TABLE matches')
    cursor.execute('ALTER TABLE matches_new RENAME TO matches')
    
    conn.commit()
    conn.close()
    print("Migration successful")

if __name__ == '__main__':
    migrate()
