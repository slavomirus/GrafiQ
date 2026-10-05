from google.oauth2 import service_account
from googleapiclient.discovery import build
import os
import datetime

# Ścieżka do pliku JSON z Service Account Key z Google Cloud
# Wymaga ustawienia zmiennej środowiskowej GOOGLE_APPLICATION_CREDENTIALS na produkcji
# Zobacz: https://developers.google.com/android-publisher/api-ref/rest
CREDENTIALS_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "google-play-key.json")
PACKAGE_NAME = os.getenv("GOOGLE_PLAY_PACKAGE_NAME", "com.grafikmobilnyzabka")

def get_play_developer_service():
    """Inicjalizuje klienta Google Play Developer API."""
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"Brak pliku klucza {CREDENTIALS_FILE}, mockowanie serwisu (tryb DEV).")
        return None
        
    credentials = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE, 
        scopes=["https://www.googleapis.com/auth/androidpublisher"]
    )
    return build('androidpublisher', 'v3', credentials=credentials)

def verify_google_play_receipt(purchase_token: str, product_id: str) -> dict:
    """
    Weryfikuje paragon subskrypcji w Google Play API.
    
    Wymagane API w GCP: Google Play Android Developer API.
    Zwraca słownik z danymi subskrypcji lub wyrzuca wyjątek w razie błędu.
    """
    service = get_play_developer_service()
    
    if not service:
        # Tychczasowy Mock na potrzeby testowania (zgodnie z punktem Open Questions w planie)
        # Zwracamy sztuczny sukces, jeśli token zaczyna się od "test_"
        if purchase_token.startswith("test_"):
            return {
                "success": True,
                "is_active": True,
                "expiry_date": datetime.datetime.utcnow() + datetime.timedelta(days=30),
                "raw_data": {"mocked": True}
            }
        else:
            return {
                "success": False,
                "is_active": False,
                "error": "Nie skonfigurowano kluczy Google Play API. Przekaż 'test_token' aby zmockować sukces."
            }

    try:
        # Wywołanie API google do pobrania statusu subskrypcji
        result = service.purchases().subscriptions().get(
            packageName=PACKAGE_NAME,
            subscriptionId=product_id,
            token=purchase_token
        ).execute()

        # paymentState: 1 = Payment received
        # Więcej na: https://developers.google.com/android-publisher/api-ref/rest/v3/purchases.subscriptions#SubscriptionPurchase
        payment_state = result.get('paymentState')
        expiry_time_millis = int(result.get('expiryTimeMillis', 0))
        
        is_active = False
        expiry_date = None
        
        if expiry_time_millis > 0:
            expiry_date = datetime.datetime.utcfromtimestamp(expiry_time_millis / 1000.0)
            if expiry_date > datetime.datetime.utcnow():
                is_active = True
                
        return {
            "success": True,
            "is_active": is_active,
            "expiry_date": expiry_date,
            "raw_data": result
        }
        
    except Exception as e:
        print(f"Błąd komunikacji z Google Play API: {e}")
        return {
            "success": False,
            "is_active": False,
            "error": str(e)
        }
