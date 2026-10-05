import json
from urllib.parse import parse_qsl, urlencode
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
import logging

logger = logging.getLogger(__name__)

def sanitize_nosql_data(data):
    """Rekursywnie usuwa klucze zaczynające się od znaku '$'."""
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            if not str(k).startswith('$'):
                sanitized[k] = sanitize_nosql_data(v)
            else:
                logger.warning(f"Zablokowano potencjalny NoSQL Injection dla klucza: {k}")
        return sanitized
    elif isinstance(data, list):
        return [sanitize_nosql_data(item) for item in data]
    return data

class NoSQLInjectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 1. Sanitizacja parametrów zapytania (Query Params)
        query_string = request.scope.get("query_string", b"").decode()
        if query_string:
            params = parse_qsl(query_string, keep_blank_values=True)
            safe_params = []
            for k, v in params:
                if not k.startswith("$"):
                    safe_params.append((k, v))
                else:
                    logger.warning(f"Zablokowano NoSQL Injection w query param: {k}")
            request.scope["query_string"] = urlencode(safe_params).encode()

        # 2. Sanitizacja ciała zapytania (Body) dla JSON
        if request.method in ["POST", "PUT", "PATCH"]:
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                try:
                    body_bytes = await request.body()
                    if body_bytes:
                        data = json.loads(body_bytes)
                        safe_data = sanitize_nosql_data(data)
                        safe_bytes = json.dumps(safe_data).encode("utf-8")
                        
                        # Zastąpienie funkcji receive, aby endpointy mogły odczytać zmodyfikowane ciało
                        async def receive():
                            return {"type": "http.request", "body": safe_bytes}
                        request._receive = receive
                except json.JSONDecodeError:
                    # Jeśli to nie jest poprawny JSON, ignorujemy
                    pass
                except Exception as e:
                    logger.error(f"Błąd podczas sanitizacji body zapytania: {e}")

        response = await call_next(request)
        return response
