import sqlite3

def get_contest_stats():
    conn = sqlite3.connect("fradodo.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT COUNT(discord_id) as total_players FROM players")
    total_players = c.fetchone()['total_players']
    
    c.execute("SELECT COUNT(id) as total_matches, SUM(kills) as total_kills, SUM(damage) as total_damage FROM matches WHERE status='approved'")
    row = c.fetchone()
    total_matches = row['total_matches'] or 0
    total_kills = row['total_kills'] or 0
    total_damage = row['total_damage'] or 0
    
    conn.close()
    return total_players, total_matches, total_kills, total_damage
