import sqlite3

def patch():
    conn = sqlite3.connect('fradodo.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS highlights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT,
            message TEXT,
            sent INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bounties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            reward_points INTEGER,
            active INTEGER DEFAULT 1
        )
    ''')
    # If bounties empty, insert a default one
    cursor.execute('SELECT COUNT(*) FROM bounties')
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO bounties (title, description, reward_points) VALUES ('Mostro della Settimana', 'Ottieni più di 30 Kills in una singola partita approvata!', 300)")
    conn.commit()
    conn.close()

if __name__ == '__main__':
    patch()
