"""Local task controls; signed previews bind intent, session, expiry and current budget."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import HTTPException, Query, Request
from fastapi import Path as ApiPath
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select

from gummy.db import attempts, operation_reports
from gummy.errors import ReportNotFound, RunConflict
from gummy.models import SourceName
from gummy.monitor import report_content_verified
from gummy.operations import (
    TaskInput,
    admission,
    cancel_task,
    enqueue,
    operations_overview,
    task_view,
)
from gummy.operations_ui import new_task_page, overview_page, preview_page, task_page
from gummy.review_routes import require_local
from gummy.workspace_ui import shell, url

COOKIE = "gummy_operation_session"
TaskId = Annotated[str, ApiPath(pattern=r"^[a-f0-9]{32}$")]


def install_operations_routes(app, db, settings, protected, respond):
    signing_key = secrets.token_bytes(32)

    def seal(session, action, value):
        data = {**value, "expires": int(time.time()) + 300, "action": action}
        encoded = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        digest = hmac.new(
            signing_key, (session + "." + encoded).encode(), hashlib.sha256
        ).hexdigest()
        return encoded + "." + digest

    def unseal(request, token, action):
        try:
            session = request.cookies.get(COOKIE, "")
            encoded, supplied = token.split(".", 1)
            expected = hmac.new(
                signing_key, (session + "." + encoded).encode(), hashlib.sha256
            ).hexdigest()
            if not session or not hmac.compare_digest(supplied, expected):
                raise ValueError
            value = json.loads(base64.urlsafe_b64decode(encoded))
            if value["action"] != action or not int(time.time()) <= value["expires"]:
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError):
            raise HTTPException(403, "確認已失效或來源不符，請重新開啟並預覽。") from None

    def form_response(request, render):
        require_local(request)
        session = request.cookies.get(COOKIE) or secrets.token_urlsafe(32)
        response = respond(render(session))
        response.set_cookie(
            COOKIE,
            session,
            max_age=3600,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    async def fields(request):
        require_local(request)
        if request.headers.get("origin") not in {None, str(request.base_url).rstrip("/")}:
            raise HTTPException(403, "請從本站表單操作。")
        if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            raise HTTPException(403, "請從本站表單操作。")
        if request.headers.get("content-type", "").split(";")[0] != (
            "application/x-www-form-urlencoded"
        ):
            raise HTTPException(415, "請使用網頁上的確認按鈕。")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                raise HTTPException(413, "表單內容過長")
        try:
            parsed = parse_qs(
                body.decode(), keep_blank_values=True, strict_parsing=True, max_num_fields=12
            )
            if any(len(v) != 1 for v in parsed.values()):
                raise ValueError
            return {k: v[0] for k, v in parsed.items()}
        except (ValueError, UnicodeError):
            raise HTTPException(422, "表單格式不符，請重新預覽。") from None

    def task_for(task_id, source):
        try:
            task = task_view(db, task_id)
            if task["source"] != source:
                raise ReportNotFound("Source mismatch")
            return task
        except ReportNotFound:
            raise HTTPException(404, "找不到此來源的任務。") from None

    @app.get("/", dependencies=protected)
    def overview(
        source: SourceName = "rapidapi", run_id: Annotated[int | None, Query(gt=0)] = None
    ):
        if run_id:
            return RedirectResponse(url("/snapshot", source=source, run_id=run_id), 303)
        return respond(overview_page(operations_overview(db, settings, source)))

    @app.get("/tasks", dependencies=protected)
    def task_list(source: SourceName = "rapidapi"):
        return respond(overview_page(operations_overview(db, settings, source), task_list=True))

    @app.get("/tasks/new", dependencies=protected)
    def new_task(
        request: Request,
        source: SourceName = "rapidapi",
        kind: str = "collect",
        run_id: Annotated[int | None, Query(gt=0)] = None,
        query: Annotated[
            str, Query(alias="search_query", min_length=1, max_length=120)
        ] = "socket wrench set",
        category: Annotated[str, Query(min_length=1, max_length=80)] = "套筒扳手",
        max_products: Annotated[int, Query(ge=1, le=2)] = 2,
    ):
        try:
            spec = TaskInput(
                source=source,
                kind=kind,
                run_id=run_id,
                query=query,
                category=category,
                max_products=max_products,
            )
        except ValidationError:
            raise HTTPException(422, "請選擇採集或報表任務。") from None
        return form_response(
            request,
            lambda session: new_task_page(source, seal(session, "preview", {}), spec.model_dump()),
        )

    @app.post("/tasks/preview", dependencies=protected)
    async def preview(request: Request):
        values = await fields(request)
        unseal(request, values.pop("csrf", ""), "preview")
        try:
            spec = TaskInput.model_validate(values)
        except ValidationError:
            raise HTTPException(422, "請確認來源、關鍵字與商品數；每批最多 2 件。") from None
        check = admission(db, settings, spec)
        return form_response(
            request,
            lambda session: preview_page(
                spec,
                check,
                seal(
                    session,
                    "confirm",
                    {
                        "spec": spec.model_dump(),
                        "id": secrets.token_hex(16),
                        "fingerprint": check["fingerprint"],
                    },
                ),
            ),
        )

    @app.post("/tasks/confirm", dependencies=protected)
    async def confirm(request: Request):
        values = await fields(request)
        if set(values) != {"token"}:
            raise HTTPException(422, "確認格式不符")
        signed = unseal(request, values["token"], "confirm")
        spec = TaskInput.model_validate(signed["spec"])
        try:
            task_id = enqueue(db, settings, spec, signed["id"], signed["fingerprint"])
        except (ValueError, RunConflict) as exc:
            from gummy.ui import cell

            return respond(
                shell(
                    "請重新預覽",
                    "/tasks",
                    spec.source,
                    '<section class="card"><h2>這次尚未建立任務</h2><p class="feedback">'
                    + cell(str(exc))
                    + '</p><a class="button" href="'
                    + url("/tasks/new", source=spec.source, kind=spec.kind, run_id=spec.run_id)
                    + '">重新預覽</a></section>',
                ),
                409,
            )
        return RedirectResponse(url("/tasks/" + task_id, source=spec.source), 303)

    @app.get("/tasks/{task_id}", dependencies=protected)
    def task_detail(request: Request, task_id: TaskId, source: SourceName = "rapidapi"):
        task = task_for(task_id, source)
        if task["kind"] in {"schedule", "schedule-report"}:
            return RedirectResponse(
                "/schedules/executions/" + task["request"]["occurrence_id"], 303
            )
        with db.engine.connect() as conn:
            outcomes = (
                [
                    f"{row['endpoint']}：{row['outcome']}"
                    for row in conn.execute(
                        select(attempts)
                        .where(attempts.c.run_id == task["run_id"])
                        .order_by(attempts.c.id)
                    ).mappings()
                ]
                if task["kind"] == "collect"
                else []
            )
        return form_response(
            request,
            lambda session: task_page(task, seal(session, "cancel:" + task_id, {}), outcomes),
        )

    @app.post("/tasks/{task_id}/cancel", dependencies=protected)
    async def stop_task(request: Request, task_id: TaskId, source: SourceName = "rapidapi"):
        values = await fields(request)
        if set(values) != {"csrf"}:
            raise HTTPException(422, "表單格式不符")
        unseal(request, values["csrf"], "cancel:" + task_id)
        task_for(task_id, source)
        try:
            cancel_task(db, task_id)
        except RunConflict:
            raise HTTPException(409, "任務狀態正在更新，請稍後再試。") from None
        return RedirectResponse(url("/tasks/" + task_id, source=source), 303)

    @app.get("/v1/operations", dependencies=protected)
    def overview_json(source: SourceName = "rapidapi"):
        return operations_overview(db, settings, source)

    @app.get("/v1/operations/tasks/{task_id}", dependencies=protected)
    def task_json(task_id: TaskId, source: SourceName = "rapidapi"):
        return task_for(task_id, source)

    @app.get("/v1/operations/reports/{task_id}.xlsx", dependencies=protected)
    def report_download(task_id: TaskId, source: SourceName = "rapidapi"):
        task = task_for(task_id, source)
        with db.engine.connect() as conn:
            row = (
                conn.execute(
                    select(operation_reports).where(operation_reports.c.task_id == task_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise HTTPException(404, "報表尚未完成")
        root = Path(__file__).resolve().parents[1]
        if task["kind"] in {"schedule", "schedule-report"} and settings.scheduler_config_path:
            root = Path(settings.scheduler_config_path).resolve().parent
        expected = root / "exports/operations" / (task_id + ".xlsx")
        path = Path(row["path"])
        if (
            path != expected
            or path.is_symlink()
            or not path.is_file()
            or path.parent.is_symlink()
            or path.resolve() != expected.absolute()
            or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]
            or not report_content_verified(path, source, task["run_id"])
        ):
            raise HTTPException(409, "報表檔案或核對紀錄已改變，暫停下載。")
        return FileResponse(
            path,
            filename=f"gummy-{source}-run{task['run_id']}.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
