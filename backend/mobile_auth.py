"""Session authentication for the app-private Android loopback server only."""
import hmac
from http.cookies import CookieError, SimpleCookie


class MobileSessionMiddleware:
    def __init__(self, app, *, token: str, port: int):
        self.app = app
        self._token = token
        self.host = f"127.0.0.1:{port}"
        self.origin = f"http://{self.host}"

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {}
        duplicate = False
        for key, value in scope.get("headers", []):
            key = key.lower()
            if key in headers and key in (b"host", b"origin", b"cookie"):
                duplicate = True
            headers[key] = value.decode("latin-1")
        origin = headers.get(b"origin")
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get(b"cookie", ""))
            supplied = cookie.get("coyote_session")
            authenticated = bool(supplied) and hmac.compare_digest(supplied.value, self._token)
        except (CookieError, TypeError, ValueError):
            authenticated = False
        allowed = (
            not duplicate and authenticated and headers.get(b"host") == self.host
            and (origin == self.origin or (kind == "http" and origin is None))
            and headers.get(b"sec-fetch-site") not in ("cross-site", "same-site")
        )
        if not allowed:
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await self._error(send, 403, b'{"error":"Forbidden"}')
            return
        if scope.get("path", "").startswith("/api/dungeon/") or scope.get("path") == "/api/content/install":
            await self._error(send, 501, b'{"error":"This feature is unavailable on Android"}')
            return

        async def protected_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-frame-options", b"DENY"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                ]
            await send(message)

        await self.app(scope, receive, protected_send)

    @staticmethod
    async def _error(send, status, body):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
