import easyocr

reader = easyocr.Reader(['it', 'en'], gpu=False)
result = reader.readtext('/home/magic/Scaricati/WhatsApp Image 2026-08-02 at 18.52.26.jpeg', detail=1)

def get_y_center(bbox):
    return (bbox[0][1] + bbox[2][1]) / 2

def get_x_center(bbox):
    return (bbox[0][0] + bbox[2][0]) / 2

target = "millokaller"
target_y = None

for bbox, text, conf in result:
    if target.lower() in text.lower():
        target_y = get_y_center(bbox)
        break

if target_y:
    row_items = []
    for bbox, text, conf in result:
        if abs(get_y_center(bbox) - target_y) < 20:
            row_items.append((get_x_center(bbox), text))
            
    row_items.sort(key=lambda x: x[0])
    texts = [item[1] for item in row_items]
    print(f"Row for {target}: {texts}")
else:
    print("Not found")
