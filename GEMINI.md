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

### ⚠️ 9. SlowAPI Rate Limiter za Reverse Proxy i brak CORS w handlerach wyjątków
* **Objaw:** Komunikat "Wystąpił nieoczekiwany błąd" przy logowaniu w aplikacji mobilnej, brak możliwości zalogowania się z telefonu lub webu.
* **Przyczyna:** Dekorator `@limiter.limit("5/15minutes")` na `POST /token` używał `get_remote_address` (`request.client.host`). Za reverse proxy (Render / Cloudflare) wszystkie żądania przychodzą ze współdzielonego IP bramy, blokując logowanie wszystkim użytkownikom po 5 próbach w 15 minut. Dodatkowo standardowy handler `_rate_limit_exceeded_handler` zwracał błąd 429 bez nagłówków CORS (`Access-Control-Allow-Origin`), co w aplikacji mobilnej objawiało się jako błąd sieciowy / "Wystąpił nieoczekiwany błąd".
* **Naprawa:**
  * Usunięto rate limiter z krytycznych endpointów autoryzacji (`/token`, `/forgot-password`).
  * Wdrożono `custom_rate_limit_exceeded_handler` z pełnymi nagłówkami CORS i formatem JSON w `main.py`.
  * Usunięto problematyczny `NoSQLInjectionMiddleware`, który ingerował w strumień `request.body()`.
  * W `dependencies.py` dodano bezpieczny fallback dla starych i testowych kont użytkowników (`free_access_until is None and subscription_valid_until is None -> is_access_valid = True`), zapobiegając fałszywym blokadom HTTP 402/403 przy `GET /users/me/profile`.

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

* **2026-10-07:**
  * **Diagnostyka i naprawa błędu logowania ("Wystąpił nieoczekiwany błąd"):**
    * **Główna przyczyna:** Dekorator `@limiter.limit("5/15minutes")` na `POST /token` używał `get_remote_address`. Za reverse proxy Rendera wszystkie żądania traktowane były jako pochodzące z jednego IP. Po 5 próbach serwer zwracał HTTP 429 przez domyślny handler SlowAPI bez nagłówków CORS (`Access-Control-Allow-Origin`), co w aplikacji mobilnej Axios maskował jako Network Error i wyświetlał "Wystąpił nieoczekiwany błąd".
    * **Backend (`auth.py`):** Usunięto `@limiter` z endpointów `/token` oraz `/forgot-password`.
    * **Backend (`main.py`):** Zastąpiono domyślny handler SlowAPI funkcją `custom_rate_limit_exceeded_handler` z pełnymi nagłówkami CORS i odpowiedzią JSON; usunięto eksperymentalny `NoSQLInjectionMiddleware` psujący strumienie `request.body()`.
    * **Backend (`dependencies.py`):** W `get_current_active_user` dodano bezpieczny fallback dla istniejących kont bez skonfigurowanych dat subskrypcji (`free_access_until is None and subscription_valid_until is None -> is_access_valid = True`), zapobiegając błędnemu rzucaniu HTTP 402/403 podczas pobierania profilu zaraz po zalogowaniu.

* **2026-10-18:**
  * **Naprawa i modernizacja systemu generowania, podglądu i pobierania plików PDF:**
    * **Backend (`reports.py`):** Naprawiono krytyczny błąd `NameError: name 'time' is not defined` (brakujący import `time` z modułu `datetime` w linii 3 rzucał HTTP 500 przy każdej próbie wygenerowania raportu godzinowego PDF w Dashboardzie i Rozliczeniach).
    * **Backend (Nagłówki i CORS):** W `reports.py`, `schedule.py` (`/month-pdf`, `/{schedule_id}/pdf`) oraz `vacation.py` ujednolicono nagłówek `Content-Disposition: inline; filename="..."` oraz dodano nagłówki `Access-Control-Allow-Origin: *` i `Access-Control-Expose-Headers: Content-Disposition`.
    * **Backend (`vacation.py`):** Zabezpieczono parsowanie dat wniosków urlopowych przed typem string bez metody `.strftime`.
    * **Frontend (`api.js` & `ScheduleScreen.js`):** Dodano metodę `scheduleAPI.getMonthlyPdfUrl(month, year)` oraz zaktualizowano `handleDownloadPDF` w `ScheduleScreen.js`, aby pobierać pełny grafik miesięczny zamiast ograniczać się do pojedynczego tygodniowego ID.
    * **Frontend (`PDFViewerScreen.js`):** Zrefaktoryzowano mechanizm podglądu na niezawodne pobieranie lokalne do cache przed renderowaniem w `<Pdf source={{ uri: fileUri }} />`, eliminując awarie sieciowe `react-native-pdf` na Androidzie; dodano akcję "Otwórz" (`FileViewer.open`) w aplikacji zewnętrznej oraz czytelny ekran błędów z przyciskiem ponowienia.
    * **Frontend (`proguard-rules.pro`):** Dodano regułę `-keep class com.ReactNativeBlobUtil.** { *; }` zapobiegającą wycinaniu klas biblioteki sieciowej w release buildzie.

* **2026-10-18 (Sesja 2):**
  * **Kompleksowe testy i naprawa systemu wymiany zmian w opublikowanym grafiku:**
    * **Backend (`validator_service.py`):** Wykryto i naprawiono błąd blokujący przejmowanie zmian z Giełdy Zmian (`take_shift`). Funkcja `validate_swap` zakładała zawsze wymianę dwustronną (1-na-1) i szukała w bazie zmiany odbiorcy z nazwą `"none"`, co odrzucało każde przejęcie z błędem `"Nie znaleziono zmiany odbiorcy"`. Dodano obsługę jednostronnego przejęcia oferty (Marketplace Takeover), weryfikując wyłącznie możliwość przypisania przejmującego pracownika do danej zmiany (`validate_shift_assignment`).
    * **Backend (`schedule.py`):** W `respond_to_shift_change_request` sformatowano `start_time` i `end_time` do spójnego formatu `"HH:MM"`, zapobiegając zapisywaniu formatu `"HH:MM:SS"`.
    * **Kompleksowe testy automatyczne:**
      * Test 1 (Prywatna wymiana 1-na-1): pomyślna weryfikacja zamiany w kolekcji płaskiej `db.schedule` oraz zagnieżdżonej `db.schedules`.
      * Test 2 (Giełda Zmian - Marketplace): pomyślna weryfikacja wystawienia oferty, listowania na giełdzie oraz przejęcia zmiany przez innego pracownika z aktualizacją obu kolekcji bazy.
      * Test 3 (Wniosek o zmianę grafiku +/- 5 dni): pomyślne złożenie wniosku przez pracownika, akceptacja przez franczyzobiorcę i utworzenie dedykowanej zmiany w opublikowanym grafiku.

* **2026-10-19:**
  * **Kompleksowa naprawa kalendarza pracownika, historii grafików i podglądu w panelu pracodawcy:**
    * **Przyczyny awarii:**
      1. **Błąd Pydantic v2 `date_from_datetime_inexact` na `end_date`:** Dokumenty grafików w MongoDB miały `end_date` zapisane jako `datetime` z czasem (np. `23:59:59`). Pydantic v2 przy typie `date` rzuca błąd walidacji, co wywoływało HTTP 500 na endpointach `GET /schedule/history`, `GET /schedules`, `GET /schedule/my-schedule` oraz `GET /schedule/{id}`.
      2. **Brakujące pola `created_at` i `status` w starszych grafikach:** Schematy `ScheduleResponse` i `ScheduleDraftResponse` wymagały tych pól bez wartości domyślnych, co powodowało błąd walidacji Pydantic `Field required` (HTTP 500).
      3. **Usuwanie klucza `_id`:** W `get_employee_schedule` linia `del schedule["_id"]` usuwała `_id`, co powodowało błędy serializacji w modelach opartych na aliasie `_id`.
      4. **Błąd "undefined is not function" w kalendarzu Dashboardu:** W `DashboardScreen.js` brakowało walidacji `Array.isArray(shifts)` oraz `Array.isArray(dateShifts)` – przy niepełnych danych zmiany wywołanie `.map()` lub `.find()` rzucało wyjątkiem JS, który był przechwytywany i wyświetlany w miejscu kalendarza jako tekst `"undefined is not a function"`. Dodatkowo `ScheduleCalendar.js` nie posiadał zabezpieczeń przed pustym `schedule` ani wywołaniem `onDayPress`.
      5. **Błąd podglądu opublikowanego grafiku (`ScheduleListScreen.js`):** Lista przekazywała `item.schedule_id` (pole niezwracane przez backend), przez co ekran edycji otwierał się z `scheduleId: undefined` i rzucał HTTP 400 (`Invalid Schedule ID`).
    * **Backend (`schemas.py`, `schedule.py`, `schedule_service.py`):**
      * W `ScheduleResponse` i `ScheduleDraftResponse` dodano `@field_validator("start_date", "end_date", mode="before")` obcinający czas z obiektów `datetime` do czystego `date`.
      * Pola `status` oraz `created_at` oznaczono jako opcjonalne z domyślnym fallbackiem (`published` dla opublikowanych, `DRAFT` dla wersji roboczych).
      * W `schedule_service.py` (`get_employee_schedule`) oraz `schedule.py` (`get_schedule_details_endpoint`) zachowano obecność zarówno `_id`, jak i `id`.
    * **Frontend Mobilny:**
      * W `ScheduleCalendar.js` dodano rygorystyczne zabezpieczenia: `safeSchedule`, bezpieczną konwersję zmian do stringa (`shift.code || shift.text`), walidację `typeof onDayPress === 'function'`.
      * W `DashboardScreen.js` dodano tablicę `MONTH_NAMES_PL` i funkcję `formatMonthYearPL` (eliminacja niestabilnego `toLocaleString`), zabezpieczono iterację po `shifts_by_date` i `dateShifts` warunkami `Array.isArray`.
      * W `ScheduleListScreen.js` naprawiono pobieranie ID: `item.id || item._id || item.schedule_id`.
      * W `EditScheduleScreen.js` zabezpieczono parametry nawigacji (`route?.params?.scheduleId`), dodano walidację ID przed zapytaniem do API oraz sprawdzanie tablic `shifts`.
      * W `SchedulePreviewModal.js` oraz `ScheduleHistoryScreen.js` zabezpieczono operacje `.split('|')` opcjonalnym chainingiem i rzutowaniem do stringa.

* **2026-10-25:**
  * **Elastyczny termin składania dyspozycji i naprawa segmentu w panelu franczyzobiorcy:**
    * **Przyczyny problemu:**
      1. **Błąd UI w polach wpisywania terminu dyspozycji:** W `StoreSettingsScreen.js` oraz `StoreSettingsComponent.js` pole `availability_deadline_day` miało przypisane `value={String(settings.availability_deadline_day || 20)}`. Przy próbie edycji i wyczyszczenia pola string stawał się pusty (`""`), co ewaluowało się do `20` i uniemożliwiało wpisanie innej wartości (analogiczny problem jak wcześniej przy `franchisee_monthly_hours`).
      2. **Brak uprawnień pracowników do odczytu ustawień sklepu (`GET /settings/store` -> HTTP 403):** Endpoint wymagał uprawnień administracyjnych (`get_current_admin_user`). Przy wejściu pracownika na ekran dyspozycji (`DyspozycjaScreen.js`) zapytanie o ustawienia sklepu kończyło się błędem 403, przez co aplikacja mobilna zawsze wpadała w fallback z twardo zakodowanym 20. dniem miesiąca.
      3. **Ograniczenie walidacji Pydantic `le=28`:** W `schemas.py` pola `availability_deadline_day` i `vacation_deadline_day` miały narzucony limit `le=28`, uniemożliwiając ustawienie terminu na dni 29–31.
    * **Backend (`settings.py`, `schemas.py`, `vacation.py`):**
      * W `settings.py` zmieniono zależność w `GET /settings/store` na `get_current_active_user`, umożliwiając wszystkim pracownikom sklepu odczytanie godzin otwarcia i terminu dyspozycji (podczas gdy `POST /settings/store` nadal wymaga `get_current_admin_user`).
      * W `schemas.py` zwiększono dopuszczalny zakres dni w `StoreSettingsBase` do `ge=1, le=31`.
      * W `vacation.py` (`get_vacation_deadline`) zastosowano dynamiczne wyznaczanie ostatniego dnia miesiąca przez `calendar.monthrange` zamiast sztywnego obcinania do 28.
    * **Frontend Mobilny (`StoreSettingsScreen.js`, `StoreSettingsComponent.js`, `DyspozycjaScreen.js`):**
      * W formularzach ustawień sklepu naprawiono `value` pola TextInput na `settings.availability_deadline_day !== undefined && settings.availability_deadline_day !== null ? String(settings.availability_deadline_day) : ''` z placeholderem `"np. 20"`, co pozwala na swobodne czyszczenie i wpisywanie dowolnego dnia (1-31).
      * W `DyspozycjaScreen.js` zaktualizowano ładowanie ustawień sklepu, wyłączono blokadę czasową dla ról franczyzobiorcy i administratora, dodano walidację przed minionymi dniami oraz wprowadzono czytelny badge informacyjny o aktualnym terminie składania dyspozycji.

  * **Kolorowy Kalendarz Grafiku Franczyzobiorcy z Multi-Selectem Pracowników (`EditScheduleScreen.js` & `ScheduleHistoryScreen.js`):**
    * **Potrzeba biznesowa:** Dotychczas franczyzobiorca w historii grafików i podglądzie miał wyłącznie widok tabelaryczny (listę). Chciał mieć możliwość wyświetlenia widoku kalendarza (identycznego jak w panelu pracownika), z filtrowaniem po pracownikach (gdzie pracownicy mogą być wybierani wielokrotnie - multi-select) oraz z kolorystycznym rozróżnieniem pracowników na siatce dni (np. Pracownik A – czerwony, Pracownik B – niebieski), co znacząco poprawia czytelność obsady sklepu.
    * **Frontend Mobilny (`EditScheduleScreen.js`):**
      * Zaimplementowano paletę 12 nasyconych, kontrastowych barw (`EMPLOYEE_PALETTE`).
      * Wprowadzono filtry pracowników z obsługą wielokrotnego wyboru (`selectedEmployeeIds` w strukturze `Set`), chipami z inicjałami, liczbą przypisanych zmian w grafiku oraz szybkimi akcjami "Zaznacz wszystkich" i "Wyczyść".
      * Zbudowano dedykowany komponent `FranchiseeScheduleCalendar` wyświetlający siatkę miesiąca z kolorowymi pillami zmian wybranych pracowników (`● JK: R`, `● AN: Z`), badge'em łącznej liczby zaplanowanych zmian oraz wskaźnikiem `+X więcej` przy większej liczbie zmian w danym dniu.
      * Dodano interaktywny modal szczegółów i edycji dnia (`DayEditModal`) otwierany po tapnięciu w dowolny dzień kalendarza z kolorowymi awatarami, etykietami godzinowymi i opcją modyfikacji obsady.
      * Zachowano przełącznik widoków: domyślny `[ 📅 Kalendarz ]` oraz `[ 📋 Tabela (Lista) ]`.
    * **Frontend Mobilny (`ScheduleHistoryScreen.js`):**
      * Ujednolicono wejście do grafiku: zarówno kliknięcie karty grafiku, jak i przycisku akcji "Podgląd / Edytuj" prowadzi bezpośrednio do `EditScheduleScreen`.
      * Usunięto przestarzały, martwy kod modalowy oraz wyeliminowano ostrzeżenia lintera ESLint.

> *Notatka dla asystenta AI:* Po zakończeniu kolejnych prac programistycznych dopisz podsumowanie zmian w tej sekcji!

