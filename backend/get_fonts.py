import os
import requests
import logging

# Konfiguracja logowania
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Ścieżka docelowa: backend/app/static/fonts
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(BASE_DIR, "app", "static", "fonts")

# Linki do oficjalnego repozytorium DejaVu Fonts
FONTS_URLS = {
    "DejaVuSans.ttf": "https://github.com/dejavu-fonts/dejavu-fonts/raw/ver_2.37/ttf/DejaVuSans.ttf",
    "DejaVuSans-Bold.ttf": "https://github.com/dejavu-fonts/dejavu-fonts/raw/ver_2.37/ttf/DejaVuSans-Bold.ttf"
}

def download_fonts():
    # Upewnij się, że katalog istnieje
    if not os.path.exists(FONTS_DIR):
        logger.info(f"Tworzenie katalogu: {FONTS_DIR}")
        os.makedirs(FONTS_DIR, exist_ok=True)
    
    for font_name, url in FONTS_URLS.items():
        file_path = os.path.join(FONTS_DIR, font_name)
        
        if os.path.exists(file_path):
            logger.info(f"Czcionka {font_name} już istnieje. Pomijanie.")
            continue
            
        logger.info(f"Pobieranie {font_name}...")
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            with open(file_path, "wb") as f:
                f.write(response.content)
            logger.info(f"✅ Pomyślnie pobrano: {font_name}")
            
        except Exception as e:
            logger.error(f"❌ Błąd podczas pobierania {font_name}: {e}")

if __name__ == "__main__":
    print("--- Rozpoczynanie pobierania polskich czcionek ---")
    download_fonts()
    print("--- Zakończono ---")
    print(f"Czcionki powinny znajdować się w: {FONTS_DIR}")
