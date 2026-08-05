import easyocr

# Questo modulo viene caricato una volta sola dal sistema Python (Singleton).
# Previene problemi di caricamento simultaneo se Discord e Web provano ad avviare OCR contemporaneamente.
print("Caricamento del modello OCR in corso (potrebbe richiedere un minuto)...")
reader = easyocr.Reader(['it', 'en'])
print("Modello OCR caricato con successo!")
