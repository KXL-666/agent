"""Browser-origin guard for loopback services; native local clients remain supported.

This is not authentication. LAN browsers must use the password-protected proxy,
which validates their original Origin before rewriting it to a loopback Origin.
"""
import ipaddress
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


def allowed_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
            return False
        # Accessing .port rejects malformed and out-of-range ports.
        _ = parsed.port
        host = parsed.hostname or ""
        if host == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LocalBrowserAccess:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            origins = [value.decode("latin-1") for name, value in scope.get("headers", []) if name == b"origin"]
            if len(origins) > 1 or (origins and not allowed_origin(origins[0])):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"detail": "不允许此浏览器来源访问本机服务"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
