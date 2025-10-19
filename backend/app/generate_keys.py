import secrets
import base64

# Generuj bezpieczny klucz szyfrowania
encryption_key = secrets.token_urlsafe(32)
print(f"ENCRYPTION_KEY={encryption_key}")

# Generuj bezpieczny secret key dla JWT
secret_key = secrets.token_urlsafe(32)
print(f"SECRET_KEY={secret_key}")

# Generuj bezpieczne hasło bazy danych
db_password = secrets.token_urlsafe(16)
print(f"DB_PASSWORD={db_password}")