import re
import os

def process_file(filepath, replacements):
    with open(filepath, 'r') as f:
        content = f.read()
    
    for old, new in replacements:
        # Use regex if old starts with re:, otherwise exact match
        if old.startswith('re:'):
            content = re.sub(old[3:], new, content)
        else:
            content = content.replace(old, new)
            
    with open(filepath, 'w') as f:
        f.write(content)

# database.py replacements
database_replacements = [
    ("total_damage INTEGER DEFAULT 0,", ""),
    ("damage INTEGER,", ""),
    ("def save_match(discord_id: str, kills: int, damage: int, placement: int,", "def save_match(discord_id: str, kills: int, placement: int,"),
    ("INSERT INTO matches (discord_id, kills, damage, placement, status, screenshot_url, ocr_confidence, api_match_id)", "INSERT INTO matches (discord_id, kills, placement, status, screenshot_url, ocr_confidence, api_match_id)"),
    ("VALUES (?, ?, ?, ?, ?, ?, ?, ?)", "VALUES (?, ?, ?, ?, ?, ?, ?)"),
    ("''', (discord_id, kills, damage, placement, status, screenshot_url, ocr_confidence, api_match_id))", "''', (discord_id, kills, placement, status, screenshot_url, ocr_confidence, api_match_id))"),
    ("total_kills = total_kills + ?, total_damage = total_damage + ?, wins = wins + ?", "total_kills = total_kills + ?, wins = wins + ?"),
    ("''', (points_earned, kills, damage, win, discord_id))", "''', (points_earned, kills, win, discord_id))"),
    ("INSERT INTO matches (discord_id, kills, damage, placement, status, screenshot_url, ocr_confidence)", "INSERT INTO matches (discord_id, kills, placement, status, screenshot_url, ocr_confidence)"),
    ("VALUES (?, ?, ?, ?, ?, ?, ?)", "VALUES (?, ?, ?, ?, ?, ?)"),
    ("''', (discord_id, kills, 0, placement, 'pending_review_contest', screenshot_url, 1.0))", "''', (discord_id, kills, placement, 'pending_review_contest', screenshot_url, 1.0))"),
    ("def resolve_review(match_id: int, action: str, new_kills: int = None, new_damage: int = None, new_placement: int = None):", "def resolve_review(match_id: int, action: str, new_kills: int = None, new_placement: int = None):"),
    ("            d = new_damage if new_damage is not None else match['damage']\n", ""),
    ("cursor.execute('UPDATE matches SET status = \"approved\", kills = ?, damage = ?, placement = ? WHERE id = ?', (k, d, p, match_id))", "cursor.execute('UPDATE matches SET status = \"approved\", kills = ?, placement = ? WHERE id = ?', (k, p, match_id))"),
    ("''', (points_earned, k, d, win, match['discord_id']))", "''', (points_earned, k, win, match['discord_id']))"),
    ("UPDATE players SET points = 0, matches_played = 0, total_kills = 0, total_damage = 0, wins = 0, contest_points = 0", "UPDATE players SET points = 0, matches_played = 0, total_kills = 0, wins = 0, contest_points = 0"),
    ("SELECT id, kills, damage, placement, status, screenshot_url FROM matches", "SELECT id, kills, placement, status, screenshot_url FROM matches"),
    ("    total_damage = 0\n", ""),
    ("            total_damage += m['damage']\n", ""),
    ("SET points = ?, contest_points = ?, matches_played = ?, total_kills = ?, total_damage = ?, wins = ?", "SET points = ?, contest_points = ?, matches_played = ?, total_kills = ?, wins = ?"),
    ("''', (points, contest_points, matches_played, total_kills, total_damage, wins, discord_id))", "''', (points, contest_points, matches_played, total_kills, wins, discord_id))"),
    ("SELECT COUNT(id) as total_matches, SUM(kills) as total_kills, SUM(damage) as total_damage", "SELECT COUNT(id) as total_matches, SUM(kills) as total_kills"),
    ("    total_damage = row['total_damage'] or 0\n", ""),
    ("    return total_players, total_matches, total_kills, total_damage", "    return total_players, total_matches, total_kills"),
    ("re:    # Maggior Danno.*?# Più Partite Giocate", "# Più Partite Giocate") # regex to remove the top_damage block using dotall? I will do it with precise replace.
]

process_file('database.py', database_replacements)
