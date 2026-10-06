# GEMINI.md – PAMIĘĆ ARCHITEKTONICZNA I DIAGNOSTYCZNA PROJEKTU GRAFIQ

> **CEL PLIKU:**  
> Minimalizacja zużycia tokenów w kolejnych sesjach poprzez zebranie pełnej wiedzy domenowej, architektonicznej, historii naprawianych błędów, pułapek (gotchas) oraz gotowych procedur naprawczych.  
> **ZASADA:** Po każdej sesji, w której wprowadzono istotne zmiany lub rozwiązano problem, ten plik NALEŻY zaktualizować.

---

## 1. STRUKTURA PROJEKTÓW I REPOZYTORIA

System składa się z dwóch niezależnych repozytoriów zintegrowanych przez REST API:

| Komponent | Lokalizacja lokalna | Repozytorium GitHub | Aktywna gałąź | Rola w systemie |
| :--- | :--- | :--- | :--- | :--- |
| **Backend API** | `/Users/everbodyhateschris/PycharmProjects/GrafiQ` | [slavomirus/GrafiQ](https://github.com/slavomirus/GrafiQ) | `main` | FastAPI, silnik bazy, generator grafików, autoryzacja, maile |
| **Frontend Mobilny** | `/Users/everbodyhateschris/PycharmProjects/GrafikMobilnyZabka` | [slavomirus/zagrafka](https://github.com/slavomirus/zagrafka) (public: `zagrafka_public`) | `frontend` | Aplikacja React Native (Android / iOS) dla franczyzobiorców i pracowników |

### Środowisko produkcyjne / Hosting
* **Backend:** Render (`https://grafiq.onrender.com`) – konteneryzacja Docker, serwer Uvicorn.
* **Baza danych:** MongoDB Atlas (asynchroniczny sterownik Python `motor`, `pymongo`).
* **Powiadomienia i Crashlytics:** Firebase (`@react-native-firebase/app`, `messaging`, `crashlytics`).
* **Subskrypcje:** RevenueCat / Google Play Billing (`react-native-purchases`, `react-native-iap`).

---

## 2. STOS TECHNOLOGICZNY I KLUCZOWE BIBLIOTEKI

### Backend (GrafiQ)
* **Python 3.11+**, **FastAPI**, **Uvicorn**, **Pydantic v2** (`pydantic-settings`).
* **Baza danych:** MongoDB Atlas, `motor.motor_asyncio.AsyncIOMotorClient`, `bson.ObjectId`.
* **Bezpieczeństwo:** `passlib.context.CryptContext` z haszowaniem `bcrypt`, `PyJWT` (algorytm HS256).
* **Cykl życia aplikacji:** `@asynccontextmanager lifespan(app: FastAPI)` w `app/main.py`.
* **Kluczowe pliki:**
  * `app/main.py` – Wejście, middleware CORS, limiter, montowanie static files.
  * `app/config.py` – Centralna konfiguracja (`Settings`) z pliku `backend/.env`.
  * `app/models.py` – Modele domenowe MongoDB i definicje enumów (`UserRole`, `ContractType`, itp.).
  * `app/schemas.py` – Schematy Pydantic v2 do walidacji żądań i odpowiedzi API.
  * `app/dependencies.py` – `get_current_user`, `get_current_active_user`, `get_current_admin_user`, obsługa multi-store.
  * `app/security.py` – `get_password_hash`, `verify_password`, `create_access_token`, `decode_token`.
  * `app/endpoints/` – Routery: `auth.py`, `users.py`, `stores.py`, `schedule.py`, `schedule_generator.py`, `vacation.py`, `availability.py`, `reports.py`, itp.
  * `app/services/` – Logika generatora (`schedule_generator_service.py`), wyliczania urlopów, zastępstw L4.

### Frontend (GrafikMobilnyZabka)
* **React 19.1.0**, **React Native 0.86.0**, **TypeScript / JavaScript**.
* **Nawigacja:** `@react-navigation/native`, `@react-navigation/native-stack` v7.
* **Storage:** `react-native-keychain` (główny bezpieczny magazyn tokenów) + fallback `@react-native-async-storage/async-storage`.
* **Komunikacja:** `axios` z interceptorami żądań i odpowiedzi (`src/services/api.js`).
* **Design & Theme:** `src/theme/colors.js` (`zabkaPalette`: granat `#003057`, żółć `#FFCC00`, jasne tło `#F5F7FA`).
* **Kluczowe pliki:**
  * `App.tsx` – Główny router z separacją ról (`FranchiseeNavigator` vs `EmployeeNavigator`) i warunkowym routingiem (onboarding, blokada płatności, weryfikacja).
  * `src/contexts/AuthContext.js` – Globalny stan użytkownika, synchronizacja danych przy logowaniu, obsługa zdarzeń `apiEventEmitter`.
  * `src/services/api.js` – Konfiguracja instancji Axios, nagłówki `Authorization: Bearer <token>` i `X-Franchise-Code`, wyłapywanie błędów 401/402/403.
  * `src/utils/tokenStorage.js` – Bezpieczny zapis i odczyt tokenów sesyjnych z Keychain.

---

## 3. CO NAJCZĘŚCIEJ ULEGA AWARII I JAK TO NAPRAWIAĆ (GOTCHAS & TROUBLESHOOTING)

### ⚠️ 1. Formatowanie czasu zmian (Time Strings vs ISO Datetime)
* **Objaw:** Wykrzaczanie się aplikacji mobilnej na ekranach grafiku (`ScheduleScreen`, `DashboardScreen`), pusty kalendarz lub błąd parsowania czasu.
* **Przyczyna:** Frontend zakłada format tekstowy `HH:MM` (często wykonując `item.start_time.substring(0, 5)`). Jeśli backend zwróci pełny ISO DateTime lub niestandardowy obiekt `time`, frontend rzuca błędem renderowania.
* **Naprawa:** Na backendzie zawsze serializować godziny zmian w formacie `"HH:MM"` (np. `14:00`, `22:00`). Na frontendzie zabezpieczyć odczyt: `(item.start_time || '').substring(0, 5)`.

### ⚠️ 2. Pydantic v2 – Walidacja i pola `None` / `null` z MongoDB
* **Objaw:** Backend zwraca HTTP 422 lub HTTP 500 przy pobieraniu profili lub list użytkowników (`UserResponse`).
* **Przyczyna:** Stare lub niepełne dokumenty w kolekcji `users` w MongoDB nie mają niektórych nowo dodanych pól (np. `store_roles`, `seniority_years`, `fte`, `employment_start_date`), albo mają wartość `null`. Jeśli w `schemas.py` pole nie jest zadeklarowane jako `Optional[Typ] = None` lub `Optional[List[...]] = Field(default_factory=list)`, Pydantic odrzuca cały dokument.
* **Naprawa:** Wszystkie pola w schematach odpowiedzi (`UserResponse`, `StoreResponse`, itp.) muszą być `Optional` z wartościami domyślnymi:
  ```python
  fte: Optional[float] = Field(default=1.0, ge=0.1, le=1.0)
  store_roles: Optional[List[str]] = Field(default_factory=list)
  leave_entitlement: Optional[int] = None
  ```

### ⚠️ 3. Umowa o pracę vs Umowa zlecenie (`fte` vs `monthly_hours_target`)
* **Objaw:** Awarie przy dodawaniu lub edycji pracownika (`AddEmployeeScreen.js`, `EmployeeManagementScreen.js`).
* **Reguła biznesowa:**
  * **Umowa o pracę (UoP):** `fte` (etat) ma wartość float (np. `1.0`, `0.75`, `0.5`, `0.25`), a `monthly_hours_target` MUSI być `null` / `None`.
  * **Umowa zlecenie (UZ):** `fte` MUSI być `null` / `None`, a `monthly_hours_target` to liczba całkowita (np. `80`, `120`).
* **Naprawa:** Przy zapisie payloadu w widokach frontendu zawsze zerować drugie pole, a na backendzie nie wymagać obecności obu naraz.

### ⚠️ 4. Custom `PyObjectId` dla Pydantic v2
* **Objaw:** Błąd serializacji JSON w FastAPI: `ValueError: [TypeError('cannot convert dictionary update sequence with size...')]` lub błędy z `ObjectId`.
* **Naprawa:** W `models.py` i `schemas.py` stosować klasę `PyObjectId` z implementacją `__get_pydantic_core_schema__`:
  ```python
  class PyObjectId(ObjectId):
      @classmethod
      def __get_pydantic_core_schema__(cls, source_type, handler):
          return core_schema.json_or_python_schema(
              json_schema=core_schema.str_schema(),
              python_schema=core_schema.union_schema([
                  core_schema.is_instance_schema(ObjectId),
                  core_schema.no_info_after_validator_function(cls.validate, core_schema.str_schema()),
              ]),
              serialization=core_schema.to_string_ser_schema(),
          )
  ```
  Zawsze używać `Field(alias="_id", serialization_alias="_id")`.

### ⚠️ 5. Przechowywanie tokena: Keychain vs AsyncStorage
* **Objaw:** Na symulatorach/emulatorach (szczególnie starszych wersjach Androida) odczyt z Keychain potrafi rzucić wyjątkiem.
* **Rozwiązanie wdrożone w `tokenStorage.js`:** Próba zapisu/odczytu z `react-native-keychain`, a w przypadku błędu przezroczysty fallback do `AsyncStorage`. Nie usuwać modułu `tokenStorage.js` ani nie zastępować go bezpośrednim `AsyncStorage.getItem('token')`.

### ⚠️ 6. Paywall i kody błędów HTTP 402 / 403
* **Objaw:** Użytkownik zostaje zablokowany na ekranie `PaymentsLock` lub `EmployeeLockoutScreen`.
* **Logika statusów:**
  * HTTP 402: Franczyzobiorca nie opłacił subskrypcji -> kierowany do `PaymentsScreen`.
  * HTTP 403 z treścią `franchise_subscription_expired`: Pracownik próbuje korzystać z systemu, ale konto jego pracodawcy (franczyzobiorcy) wygasło -> kierowany do `EmployeeLockoutScreen`.
* **W celach testowych / deweloperskich:** W `dependencies.py` można tymczasowo nadać `free_access_until` w przyszłości lub użyć referral code (daje 90 dni darmowego dostępu).

### ⚠️ 7. Nagłówek `X-Franchise-Code` i przełączanie sklepów (Multi-store)
* **Objaw:** Zapytania backendowe zwracają dane innego sklepu lub błąd 403 przy zapytaniach o grafik.
* **Mechanizm:** Franczyzobiorca może posiadać wiele placówek (`franchise_codes: List[str]`). Aktywny sklep jest zapisany w `AsyncStorage` jako `selected_franchise_code` i wstrzykiwany przez interceptor w `api.js` jako nagłówek `X-Franchise-Code`. Backend w `dependencies.py` weryfikuje dostęp usera do tego kodu i ustawia `current_user["franchise_code"]`. Jeśli nagłówek wskazuje na nieobsługiwany sklep, następuje bezpieczny fallback do pierwszego dostępnego sklepu użytkownika.

### ⚠️ 8. Renderowanie obiektów w React Native (błąd "Objects are not valid as a React child")
* **Objaw:** Czerwony ekran z błędem renderowania obiektu zamiast stringa.
* **Historia napraw:** Miało to miejsce m.in. w module zgłaszania L4 (`AdminPanelScreen.js`), gdzie pole zastępcy `replacement` z backendu bywało obiektem `{first_name, last_name, _id}` zamiast czystego stringa.
* **Zasada:** Wszelkie dane pobierane z backendu, które mają trafić do komponentu `<Text>`, muszą być rzutowane lub zabezpieczone:
  ```javascript
  const displayName = typeof rep.replacement === 'object' && rep.replacement 
      ? `${rep.replacement.first_name} ${rep.replacement.last_name}`
      : String(rep.replacement_name || rep.replacement || 'Nieznany');
  ```

---

## 4. PROCEDURY OPERACYJNE I PRZYDATNE KOMENDY

### Uruchamianie backendu lokalnie
```bash
cd /Users/everbodyhateschris/PycharmProjects/GrafiQ/backend
source venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
* Sprawdzenie zarejestrowanych tras w przeglądarce: `GET http://localhost:8000/routes`
* Dokumentacja OpenAPI Swagger: `GET http://localhost:8000/docs`

### Uruchamianie frontendu (React Native)
```bash
cd /Users/everbodyhateschris/PycharmProjects/GrafikMobilnyZabka

# Uruchomienie Metro Bundler z czyszczeniem cache:
npm run start -- --reset-cache

# Uruchomienie na Androidzie:
npm run android

# Uruchomienie na iOS (Mac):
cd ios && pod install && cd ..
npm run ios
```

### Git – typowe operacje
* Wypchnięcie zmian backendu:
  ```bash
  cd /Users/everbodyhateschris/PycharmProjects/GrafiQ
  git add . && git commit -m "Opis zmian" && git push origin main
  ```
* Wypchnięcie zmian frontendu:
  ```bash
  cd /Users/everbodyhateschris/PycharmProjects/GrafikMobilnyZabka
  git add . && git commit -m "Opis zmian" && git push origin frontend
  ```

---

## 5. REJESTR ZMIAN I HISTORIA NAPRAW (DLA KOLEJNYCH SESJI)

* **2026-09-27:**
  * Opracowano pełną dokumentację 3 laboratoriów akademickich ([SPRAWOZDANIA_LABORATORIA.md](file:///Users/everbodyhateschris/PycharmProjects/GrafiQ/SPRAWOZDANIA_LABORATORIA.md)) z podziałem na Frontend SPA, Backend REST API i Bezpieczeństwo.
  * Zlinkowano repozytoria GitHub: `slavomirus/GrafiQ` (backend, gałąź `main`) oraz `slavomirus/zagrafka` (frontend, gałąź `frontend`).
* **2026-10-04:**
  * Utworzono plik `GEMINI.md` jako centralną bazę wiedzy i pamięć podręczną projektu, zapobiegając nadmiarowemu zużyciu tokenów.
  * Zestawiono rejestr typowych awarii (formatowanie `HH:MM`, obsługa `null` w `fte`, migracja do Keychain, obsługa paywalla 402/403).
  * **Integracja Subskrypcji i Płatności RevenueCat + Google Play:**
    * Połączono Google Play Console Service Account z RevenueCat (uprawnienia aplikacji i finansowe).
    * Backend: Poprawiono weryfikację subskrypcji w `dependencies.py` – sprawdzanie `is_subscription_active` (pole ustawiane przez webhook RevenueCat) oraz `isPremium`.
    * Backend: Rozbudowano `webhooks.py` o obsługę `PRODUCT_CHANGE` oraz automatyczny zapis `subscription_plan` (Google Play SKU) i `isPremium: True/False`.
    * Frontend: Wdrożono pełny serwis `revenueCatService.js` oparty na `react-native-purchases` (SDK v10.4.0) z metodami `initialize`, `logIn(userId)`, `getOfferings()`, `purchasePackage()`, `restorePurchases()`.
    * Frontend: Przepisano `PaymentsScreen.js` z niedokończonego `react-native-iap` na RevenueCat SDK, zachowując działający system kodów polecających.
    * Frontend: Zsynchronizowano powiązanie sesji użytkownika w `AuthContext.js` (`Purchases.logIn(userId)` przy starcie/logowaniu, `Purchases.logOut()` przy wylogowaniu, dodano `refreshUser()`).

* **2026-10-06:**
  * **Naprawa i modernizacja Monitora Dyspozycji oraz Powiadomień / Przypomnień:**
    * **Backend (`availability.py`):**
      * Zaimplementowano brakujący endpoint `GET /availability/user/{user_id}` (naprawa błędu weryfikacji dyspozycji w Dashboardzie pracownika).
      * Zrefaktoryzowano `GET /availability/monitor/status`: obsługa multi-store (`franchise_codes` i `franchise_code`), case-insensitive `role`, obsługa `ObjectId` i `string` ID oraz dat w formatach `datetime` i `str` (YYYY-MM-DD), dodano zliczanie dni dyspozycji (`declared_days_count`).
      * Zmodernizowano `POST /availability/monitor/remind`: wysyłka push przez Firebase FCM oraz niezawodny fallback/uzupełnienie mailowe przez nową funkcję `send_availability_reminder_email` w `email_service.py`.
      * W `schemas.py` oznaczono pole `submitted_at` w `Availability` jako `Optional[datetime] = None`.
    * **Frontend (`ScheduleGenerationScreen.js`):**
* **2026-10-06 (Sesja 2):**
  * **Naprawa i modernizacja generatora plików PDF oraz ich podglądu/pobierania:**
    * **Przyczyna awarii:** Kontenery produkcyjne Render (`python:3.11-slim-bookworm`) nie posiadały zainstalowanych fontów TrueType, a katalog `backend/app/static/fonts/` nie istniał w repozytorium. Błędny fallback do `TTFont("Helvetica")` rzucał `TTFError` / `ValueError` ("Can't map determine family/bold/italic for dejavusans") przy każdej próbie budowy dokumentu ReportLab (HTTP 500).
    * **Backend (`pdf_service.py` & `Dockerfile`):**
      * Skonfigurowano i dołączono do repozytorium pliki czcionek TTF (`DejaVuSans.ttf`, `DejaVuSans-Bold.ttf`) w `app/static/fonts/` z pełną obsługą polskich znaków diakrytycznych.
      * W `backend/Dockerfile` dodano instalację pakietu `fonts-dejavu-core`.
      * Wprowadzono bezpieczny fallback do standardowych czcionek Type 1 ReportLab (`Helvetica` / `Helvetica-Bold`) w razie braku plików TTF, eliminując ryzyko awarii serwera.
      * Naprawiono funkcję `is_on_sick_leave` (bezpieczne rzutowanie dat bez `TypeError: 'hour' is an invalid keyword argument for date`).
      * Zabezpieczono parsowanie dat w `generate_hours_report_pdf` przed `AttributeError: 'str' object has no attribute 'strftime'`.
    * **Backend (Autoryzacja i Endpointy):**
      * Wprowadzono uniwersalną zależność `get_current_user_query` w `dependencies.py` (obsługa `?token=...`, `?franchise_code=...` oraz nagłówków Bearer i `X-Franchise-Code`).
      * W `schedule.py` odblokowano możliwość pobierania opublikowanego grafiku PDF (`/{schedule_id}/pdf` oraz `/month-pdf`) dla pracowników przypisanych do sklepu oraz dodano obsługę `franchise_codes` (multi-store).
      * W `vacation.py` odblokowano pobieranie wniosku PDF (`/{vacation_id}/pdf`) dla pracownika, który go złożył.
      * W `reports.py` (`/hours/pdf`) zintegrowano autoryzację query param oraz obsługę `user_id` w formatach `ObjectId` i `str`.
    * **Frontend Mobilny:**
      * W `PDFViewerScreen.js` dodano automatyczne wstrzykiwanie `token` i `franchise_code` do adresu URL, walidację statusu HTTP (`res.info().status < 400`) oraz wsparcie pobierania/otwierania na platformach iOS i Android.
      * W `EditScheduleScreen.js` zastąpiono `Linking.openURL` bezpośrednią, płynną nawigacją do komponentu `PDFViewer`.
      * W `ScheduleHistoryScreen.js` dodano query token fallback oraz naprawiono nawigację do `EditSchedule`.

> *Notatka dla asystenta AI:* Po zakończeniu kolejnych prac programistycznych dopisz podsumowanie zmian w tej sekcji!

