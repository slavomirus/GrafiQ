FROM python:3.11-slim-bookworm

WORKDIR /app

# Instalacja narzędzi i certyfikatów
RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Pobierz certyfikaty CA dla MongoDB Atlas
RUN curl -sS https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem -o /etc/ssl/certs/mongodb-atlas-ca.pem

# Alternatywnie: pobierz certyfikaty GlobalSign (dostawca MongoDB Atlas)
RUN curl -sS https://secure.globalsign.com/cacert/Root-R1.pem -o /etc/ssl/certs/globalsign-root-r1.pem
RUN curl -sS https://secure.globalsign.com/cacert/Root-R2.pem -o /etc/ssl/certs/globalsign-root-r2.pem
RUN curl -sS https://secure.globalsign.com/cacert/Root-R3.pem -o /etc/ssl/certs/globalsign-root-r3.pem

# Aktualizacja certyfikatów systemowych
RUN update-ca-certificates -f

COPY requirements.txt .
# Brutalnie usuwamy wszystkie stare wersje bcrypt/py-bcrypt, aby mieć pewność
RUN pip uninstall -y py-bcrypt bcrypt

# Instalujemy zależności, co teraz zmusi pip do pobrania nowej, poprawnej wersji bcrypt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]