import os
import requests

PIXABAY_API_KEY = "49588630-6bcf348ad86987cc70639da3f"
WORDS = ["cat", "dog", "fish", "car", "tree", "sun", "apple", "book"]
SAVE_DIR = "static/images"

os.makedirs(SAVE_DIR, exist_ok=True)

def fetch_images(word, count=3):
    base_url = "https://pixabay.com/api/"
    params = {
        "key": PIXABAY_API_KEY,
        "q": word,
        "image_type": "photo",
        "per_page": str(count),
        "safesearch": "true"
    }

    try:
        response = requests.get(base_url, params=params)
        print(f"[DEBUG] Request URL: {response.url}")
        response.raise_for_status()
        data = response.json()
        hits = data.get("hits", [])

        if not hits:
            print(f"[ERROR] Ei löytynyt kuvia sanalle: {word}")
            return

        for i, hit in enumerate(hits[:count]):
            img_url = hit["webformatURL"]
            img_data = requests.get(img_url).content
            filename = os.path.join(SAVE_DIR, f"{word}_{i+1}.jpg")
            with open(filename, "wb") as f:
                f.write(img_data)
            print(f"[OK] Tallennettu: {filename}")

    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] {word}: {e}")

for word in WORDS:
    fetch_images(word)
