"""Same-origin local forms with session-bound, short-lived signed intent."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qs

from fastapi import HTTPException

from gummy.review_routes import require_local


class LocalForms:
    def __init__(self, name):
        self.cookie = "gummy_" + name + "_session"
        self.key = secrets.token_bytes(32)

    def seal(self, session, action, value):
        data = {**value, "action": action, "expires": int(time.time()) + 300}
        encoded = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        sig = hmac.new(self.key, (session + "." + encoded).encode(), hashlib.sha256).hexdigest()
        return encoded + "." + sig

    def unseal(self, request, token, action):
        try:
            session = request.cookies.get(self.cookie, "")
            encoded, supplied = token.split(".", 1)
            sig = hmac.new(self.key, (session + "." + encoded).encode(), hashlib.sha256).hexdigest()
            if not session or not hmac.compare_digest(sig, supplied):
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(encoded))
            if data["action"] != action or data["expires"] < int(time.time()):
                raise ValueError
            return data
        except (ValueError, TypeError, KeyError):
            raise HTTPException(403, "確認已失效，請重新預覽。") from None

    def response(self, request, respond, render):
        require_local(request)
        session = request.cookies.get(self.cookie) or secrets.token_urlsafe(32)
        response = respond(render(session))
        response.set_cookie(
            self.cookie,
            session,
            max_age=3600,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    async def fields(self, request):
        require_local(request)
        if request.headers.get("origin") not in {
            None,
            str(request.base_url).rstrip("/"),
        } or request.headers.get("sec-fetch-site") not in {None, "none", "same-origin"}:
            raise HTTPException(403, "請從本站表單操作。")
        if (
            request.headers.get("content-type", "").split(";")[0]
            != "application/x-www-form-urlencoded"
        ):
            raise HTTPException(415, "請使用本站表單。")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 32768:
                raise HTTPException(413, "表單過長")
        try:
            parsed = parse_qs(
                content.decode(), keep_blank_values=True, strict_parsing=True, max_num_fields=140
            )
            if any(len(v) != 1 for v in parsed.values()):
                raise ValueError
            return {k: v[0] for k, v in parsed.items()}
        except (UnicodeError, ValueError):
            raise HTTPException(422, "表單格式不符。") from None
