"""Local-only form writes with signed, session- and revision-bound CSRF protection."""

import hashlib
import hmac
import ipaddress
import secrets
import time
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from gummy.catalog import CatalogQuery, catalog_data
from gummy.errors import ReportNotFound
from gummy.models import SourceName
from gummy.review_ui import detail_page, queue_page
from gummy.reviews import ReviewConflict, ReviewEdit, review_subject, save_review

COOKIE = "gummy_review_session"
Asin = Annotated[str, Path(pattern=r"^[A-Z0-9]{10}$")]
RunId = Annotated[int, Path(gt=0, lt=2**63)]


def require_local(request):
    try:
        local = ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        local = False
    if not local or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(403, "人工核對寫入限本機使用；請從本機網址開啟。")


def install_review_routes(app, db, protected, respond):
    signing_key = secrets.token_bytes(32)

    def signature(session, source, run_id, asin, revision, digest, expires):
        message = "|".join(map(str, [session, source, run_id, asin, revision, digest, expires]))
        return hmac.new(signing_key, message.encode(), hashlib.sha256).hexdigest()

    def subject_for(source, run_id, asin):
        try:
            return review_subject(db, source, run_id, asin)
        except ReportNotFound:
            raise HTTPException(404, "找不到這個來源的商品批次") from None

    def form_response(request, subject, draft=None, message="", status=200, saved=False):
        session = request.cookies.get(COOKIE) or secrets.token_urlsafe(32)
        expires = int(time.time()) + 3600
        revision = subject["latest"]["revision"] if subject["latest"] else 0
        sig = signature(
            session,
            subject["run"]["source"],
            subject["run"]["id"],
            subject["product"]["asin"],
            revision,
            subject["fingerprint"],
            expires,
        )
        response = respond(detail_page(subject, f"{expires}.{sig}", draft, message, saved), status)
        response.set_cookie(
            COOKIE,
            session,
            max_age=3600,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/reviews",
        )
        return response

    @app.get("/reviews", dependencies=protected)
    def reviews(query: Annotated[CatalogQuery, Query()]):
        query = query.model_copy(update={"view": "observations"})
        try:
            return respond(queue_page(catalog_data(db, query)))
        except ValueError:
            raise HTTPException(422, "資料範圍過大，請縮小核對範圍") from None

    @app.get("/reviews/{run_id}/{asin}", dependencies=protected)
    def review_form(
        request: Request,
        run_id: RunId,
        asin: Asin,
        source: SourceName = "rapidapi",
        saved: bool = False,
    ):
        require_local(request)
        return form_response(request, subject_for(source, run_id, asin), saved=saved)

    @app.post("/reviews/{run_id}/{asin}", dependencies=protected)
    async def store_review(
        request: Request, run_id: RunId, asin: Asin, source: SourceName = "rapidapi"
    ):
        require_local(request)
        if request.headers.get("origin") not in {None, str(request.base_url).rstrip("/")}:
            raise HTTPException(403, "表單來源不符，請重新開啟本機核對頁。")
        if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            raise HTTPException(403, "請從本站核對表單儲存。")
        if (
            request.headers.get("content-type", "").split(";")[0]
            != "application/x-www-form-urlencoded"
        ):
            raise HTTPException(415, "請使用核對頁的儲存按鈕。")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 32768:
                raise HTTPException(413, "表單內容過長")
        try:
            parsed = parse_qs(
                body.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=40
            )
            if any(len(values) != 1 for values in parsed.values()):
                raise ValueError("Repeated fields")
            fields = {key: values[0] for key, values in parsed.items()}
            token = fields.pop("csrf")
            expires, supplied = token.split(".", 1)
            session = request.cookies.get(COOKIE, "")
            expected = signature(
                session,
                source,
                run_id,
                asin,
                fields.get("revision"),
                fields.get("fingerprint"),
                expires,
            )
            if (
                not session
                or not int(time.time()) <= int(expires) <= int(time.time()) + 3600
                or not hmac.compare_digest(expected, supplied)
            ):
                raise ValueError("Invalid form token")
        except (ValueError, KeyError, UnicodeError):
            raise HTTPException(403, "表單已失效或來源不符。請返回核對頁重新開啟。") from None
        subject = subject_for(source, run_id, asin)
        draft = dict(fields)
        try:
            if any(key.startswith("sample_") for key in fields):
                raise ValueError("評論已改為唯讀來源資料，不再接受人工標註。請重新整理此頁。")
            edit = ReviewEdit.model_validate(fields)
            save_review(db, source, run_id, asin, edit)
        except ReviewConflict as exc:
            return form_response(request, subject_for(source, run_id, asin), draft, str(exc), 409)
        except (ValidationError, ValueError) as exc:
            message = (
                "請選擇有效的核對判斷，填寫 1–80 字名稱；核對備註最多 2,000 字。"
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            return form_response(request, subject, draft, message, 422)
        return RedirectResponse(
            f"/reviews/{run_id}/{asin}?source={source}&saved=true",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )
