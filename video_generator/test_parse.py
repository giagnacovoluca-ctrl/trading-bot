import json
import re
import ast

def clean_slide(s):
    if isinstance(s, dict):
        testo = s.get('testo_schermo', s.get('text', s.get('testo', s.get('content', ''))))
        if not testo:
            testo = str(s)
    elif isinstance(s, str):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                testo = parsed.get('testo_schermo', parsed.get('text', parsed.get('testo', parsed.get('content', s))))
            else:
                testo = s
        except:
            try:
                parsed = ast.literal_eval(s)
                if isinstance(parsed, dict):
                    testo = parsed.get('testo_schermo', parsed.get('text', parsed.get('testo', parsed.get('content', s))))
                else:
                    testo = s
            except:
                testo = s
    else:
        testo = str(s)
        
    if not isinstance(testo, str):
        testo = str(testo)
        
    testo = re.sub(r'<[^>]+>', '', testo)
    testo = re.sub(r'\*\*(.*?)\*\*', r'\1', testo)
    testo = re.sub(r'\*(.*?)\*', r'\1', testo)
    return testo

print(clean_slide("{'slide_number': 1, 'testo_schermo': 'Tutti i libri di neuroscienze sono appena stati riscritti. (Non sto esagerando)', 'descrizione_visiva': 'Stile ipnotico e misterioso.'}"))
