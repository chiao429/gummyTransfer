import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text

from gummy.analysis import AnalysisQuery, analysis_csv, analysis_data
from gummy.analysis_ui import analysis_page
from gummy.catalog import CatalogQuery, catalog_csv, catalog_data
from gummy.config import Settings
from gummy.db import Database
from gummy.errors import ReportNotFound
from gummy.local_settings import credential_status
from gummy.models import CombinationQuery, SourceName
from gummy.monitor import active_plan_overview
from gummy.operations_routes import install_operations_routes
from gummy.reference import reference_coverage
from gummy.report_routes import install_report_routes
from gummy.reports import (
    combinations,
    export_combinations_csv,
    export_csv,
    export_source_audit_csv,
    report_metadata,
    serialize_product,
    source_audit_rows,
)
from gummy.review_routes import install_review_routes
from gummy.schedule_routes import install_schedule_routes
from gummy.ui import dashboard
from gummy.verification import verify_snapshot
from gummy.workbook_data import portfolio_payload
from gummy.workspace_ui import export_page, periods_page, products_page, settings_page, shell


def create_app(settings: Settings | None = None, db: Database | None = None):
    settings = settings or Settings.from_env()
    if settings.app_env == "cloud":
        settings.validate_bind("0.0.0.0")
    owns_db = db is None
    db = db or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        if settings.app_env == "local":
            db.initialize()
        yield
        if owns_db:
            db.engine.dispose()

    app = FastAPI(
        title="gummy Amazon MVP",
        version="0.1.0",
        lifespan=lifespan,
        description=(
            "Read persisted snapshots. Demo data is artificial. API reads never collect data."
        ),
        docs_url="/docs" if settings.app_env == "local" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.app_env == "local" else None,
    )
    bearer = HTTPBearer(auto_error=False)

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if settings.api_token and (
            credentials is None
            or not secrets.compare_digest(credentials.credentials, settings.api_token)
        ):
            raise HTTPException(
                status_code=401, detail="Invalid API token", headers={"WWW-Authenticate": "Bearer"}
            )

    protected = [Depends(authenticate)]

    def workspace_response(content: str, status_code: int = 200):
        return HTMLResponse(
            content,
            status_code=status_code,
            headers={
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
                ),
                "X-Content-Type-Options": "nosniff",
                # Preserve same-origin POST Origin in browsers; cross-origin links
                # still receive no referrer. Null Origin remains rejected on writes.
                "Referrer-Policy": "same-origin",
                "Cache-Control": "no-store",
            },
        )

    def selected_catalog(query):
        try:
            return catalog_data(db, query)
        except ValueError:
            raise HTTPException(status_code=422, detail="資料範圍過大，請縮小日期範圍") from None

    def selected_analysis(query):
        try:
            return analysis_data(db, query)
        except ValueError:
            raise HTTPException(status_code=422, detail="資料範圍過大，請縮小日期範圍") from None

    @app.exception_handler(RequestValidationError)
    async def invalid_query(request: Request, exc):
        if request.url.path in {
            "/products",
            "/reviews",
            "/periods",
            "/exports/preview",
            "/v1/exports/catalog.csv",
            "/analysis",
            "/reports",
            "/v1/exports/analysis.csv",
            "/v1/exports/analysis-products.csv",
        }:
            is_analysis = request.url.path in {
                "/analysis",
                "/v1/exports/analysis.csv",
                "/v1/exports/analysis-products.csv",
            }
            is_report = request.url.path == "/reports"
            return_path = "/reports" if is_report else "/analysis" if is_analysis else "/products"
            return_label = (
                "返回報表中心" if is_report else "返回取樣分析" if is_analysis else "返回商品資料"
            )
            return workspace_response(
                shell(
                    "請調整查詢條件",
                    return_path,
                    "rapidapi",
                    '<section class="card"><h2>尚未完成查詢或匯出</h2><p class="feedback">'
                    "請確認開始日期不晚於結束日期、最低價格不高於最高價格，"
                    "且價格為非負數、最多兩位小數。統計方式與價格帶請使用提供的選項；"
                    "取樣分析只接受每件商品最新一筆。匯出至少需要選擇一個欄位。</p><p>你可以使用瀏覽器返回保留剛才的選擇，或重新查詢。</p>"
                    f'<a class="button secondary" href="{return_path}">'
                    f"{return_label}</a></section>",
                ),
                422,
            )
        return await request_validation_exception_handler(request, exc)

    def portfolio_for(source: SourceName, through_run_id: int | None = None) -> dict:
        selected, _ = db.report(source, through_run_id)
        snapshots = []
        for selected_id in db.successful_run_ids(source, selected["id"]):
            run, rows = db.report(source, selected_id)
            snapshots.append((run, rows, db.evidence(selected_id)))
        return portfolio_payload(
            snapshots,
            reference_coverage(db.category_stats(source, selected["id"]), source),
        )

    if settings.app_env == "local":
        install_review_routes(app, db, protected, workspace_response)
        install_operations_routes(app, db, settings, protected, workspace_response)
        install_schedule_routes(app, db, settings, protected, workspace_response)
        install_report_routes(app, db, settings, protected, workspace_response)

        @app.get("/products", response_class=HTMLResponse, dependencies=protected)
        def browse_products(query: Annotated[CatalogQuery, Query()]):
            return workspace_response(products_page(selected_catalog(query)))

        @app.get("/periods", response_class=HTMLResponse, dependencies=protected)
        def browse_periods(query: Annotated[CatalogQuery, Query()]):
            # Period views contain all observations of each selected successful batch.
            scoped = CatalogQuery(
                source=query.source,
                date_from=query.date_from,
                date_to=query.date_to,
                snapshot=query.snapshot,
                view="observations",
            )
            return workspace_response(
                periods_page(selected_catalog(scoped), active_plan_overview(settings, db))
            )

        @app.get("/analysis", response_class=HTMLResponse, dependencies=protected)
        def browse_analysis(query: Annotated[AnalysisQuery, Query()]):
            return workspace_response(analysis_page(selected_analysis(query)))

        @app.get("/exports/preview", response_class=HTMLResponse, dependencies=protected)
        def preview_export(query: Annotated[CatalogQuery, Query()]):
            return workspace_response(export_page(selected_catalog(query)))

        @app.get("/settings", response_class=HTMLResponse, dependencies=protected)
        def configuration_page():
            try:
                latest_run, _ = db.report("rapidapi")
            except ReportNotFound:
                latest_run = None
            return workspace_response(
                settings_page(
                    credential_status(settings),
                    db.quota_status(settings.total_limit, settings.daily_limit),
                    db.provider_quota_status(settings.rapidapi_key),
                    latest_run,
                    active_plan_overview(settings, db),
                )
            )

        @app.get("/v1/exports/workbooks/{run_id}.xlsx", dependencies=protected)
        def download_period_workbook(run_id: int, source: SourceName = "rapidapi"):
            plan = active_plan_overview(settings, db)
            report = next(
                (
                    batch.get("report")
                    for batch in plan.get("batches", [])
                    if batch.get("run_id") == run_id
                ),
                None,
            )
            if plan.get("source") != source or not report or not report["exists"]:
                raise HTTPException(status_code=404, detail="No workbook for this batch")
            path = Path(report["path"])
            if path.suffix != ".xlsx" or path.is_symlink() or not path.is_file():
                raise HTTPException(status_code=404, detail="Workbook unavailable")
            return FileResponse(
                path,
                filename=path.name,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )

        @app.get("/snapshot", response_class=HTMLResponse, dependencies=protected)
        def dashboard_home(
            source: SourceName = "rapidapi",
            run_id: Annotated[int | None, Query(gt=0)] = None,
        ):
            quota = db.quota_status(settings.total_limit, settings.daily_limit)
            try:
                run, rows = db.report(source, run_id)
            except ReportNotFound:
                run, rows = None, []
            grouped = (
                combinations(
                    run,
                    rows,
                    CombinationQuery(
                        source=source,
                        run_id=run["id"],
                        include_title_candidates=True,
                    ),
                )
                if run
                else None
            )
            evidence = db.evidence(run["id"]) if run else {}
            evidence_count = len(evidence)
            verification = verify_snapshot(run, rows, evidence) if run else None
            portfolio = portfolio_for(source) if run else None
            return HTMLResponse(
                dashboard(
                    source,
                    run,
                    rows,
                    quota,
                    grouped,
                    evidence_count,
                    verification,
                    db.recent_runs(10),
                    source_audit_rows(run, rows, evidence) if run else [],
                    reference_coverage(db.category_stats("rapidapi"), "rapidapi"),
                    portfolio,
                    active_plan_overview(settings, db),
                    evidence,
                ),
                headers={
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                },
            )

        @app.get("/v1/plan", dependencies=protected)
        def current_plan():
            return active_plan_overview(settings, db)

        @app.get("/v1/exports/latest-workbook.xlsx", dependencies=protected)
        def download_latest_workbook(source: SourceName = "rapidapi"):
            # The server selects the installed plan's report; never accept a file path.
            plan = active_plan_overview(settings, db)
            report = plan.get("latest_report")
            if plan.get("source") != source or not report or not report["exists"]:
                raise HTTPException(status_code=404, detail="No daily workbook for this source")
            path = Path(report["path"])
            if path.suffix != ".xlsx" or path.is_symlink() or not path.is_file():
                raise HTTPException(status_code=404, detail="Daily workbook unavailable")
            return FileResponse(
                path,
                filename=path.name,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )

    @app.exception_handler(ReportNotFound)
    async def missing_report(_, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        try:
            with db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            raise HTTPException(status_code=503, detail="Database unavailable") from None
        return {"status": "ok", "service": "gummy"}

    @app.get("/v1/reports/latest", dependencies=protected)
    def latest(source: SourceName = "demo"):
        return report_metadata(*db.report(source))

    @app.get("/v1/reports/verification", dependencies=protected)
    def report_verification(
        source: SourceName = "demo",
        run_id: Annotated[int | None, Query(gt=0)] = None,
    ):
        run, rows = db.report(source, run_id)
        return verify_snapshot(run, rows, db.evidence(run["id"]))

    @app.get("/v1/products", dependencies=protected)
    def product_list(
        source: SourceName = "demo",
        run_id: Annotated[int | None, Query(gt=0)] = None,
    ):
        run, rows = db.report(source, run_id)
        return {
            "report": report_metadata(run, rows),
            "products": [serialize_product(row) for row in rows],
        }

    @app.get("/v1/portfolio", dependencies=protected)
    def portfolio_list(
        source: SourceName = "rapidapi",
        through_run_id: Annotated[int | None, Query(gt=0)] = None,
    ):
        return portfolio_for(source, through_run_id)

    @app.post("/v1/combinations/query", dependencies=protected)
    def combination_query(query: CombinationQuery):
        run, rows = db.report(query.source, query.run_id)
        return combinations(run, rows, query)

    @app.get("/v1/exports/products.csv", dependencies=protected)
    def download(
        source: SourceName = "demo",
        run_id: Annotated[int | None, Query(gt=0)] = None,
    ):
        run, rows = db.report(source, run_id)
        return Response(
            export_csv(run, rows),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="gummy-{source}-{run["id"]}.csv"'
            },
        )

    @app.get("/v1/exports/combinations.csv", dependencies=protected)
    def download_combinations(
        source: SourceName = "demo",
        run_id: Annotated[int | None, Query(gt=0)] = None,
        include_title_candidates: bool = False,
    ):
        run, rows = db.report(source, run_id)
        query = CombinationQuery(
            source=source,
            run_id=run["id"],
            include_title_candidates=include_title_candidates,
        )
        filename = f"gummy-{source}-{run['id']}-combinations.csv"
        return Response(
            export_combinations_csv(run, rows, query),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/v1/exports/audit.csv", dependencies=protected)
    def download_audit(
        source: SourceName = "demo",
        run_id: Annotated[int | None, Query(gt=0)] = None,
    ):
        run, rows = db.report(source, run_id)
        filename = f"gummy-{source}-{run['id']}-audit.csv"
        return Response(
            export_source_audit_csv(run, rows, db.evidence(run["id"])),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/v1/quota", dependencies=protected)
    def quota():
        return db.quota_status(settings.total_limit, settings.daily_limit)

    @app.get("/v1/catalog", dependencies=protected)
    def catalog_list(query: Annotated[CatalogQuery, Query()]):
        data = selected_catalog(query)
        return {
            "scope": data["query"].model_dump(mode="json"),
            "date_timezone": "Asia/Taipei",
            "date_basis": "batch_completed_at",
            "total": data["total"],
            "distinct_asins": data["distinct_asins"],
            "page": data["page"],
            "pages": data["pages"],
            "products": data["page_rows"],
        }

    @app.get("/v1/exports/catalog.csv", dependencies=protected)
    def download_catalog(query: Annotated[CatalogQuery, Query()]):
        data = selected_catalog(query)
        return Response(
            catalog_csv(data),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="gummy-{query.source}-selection.csv"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/v1/analysis", dependencies=protected)
    def sample_analysis(query: Annotated[AnalysisQuery, Query()]):
        data = selected_analysis(query)
        return {
            "scope": data["query"].model_dump(mode="json"),
            "date_timezone": "Asia/Taipei",
            "date_basis": "batch_completed_at",
            "latest_selection_basis": data["latest_selection_basis"],
            "sample_scope": "sampled_products_only",
            "sales_metric_available": data["sales_metric_available"],
            "summary": data["summary"],
            "groups": data["groups"],
        }

    @app.get("/v1/exports/analysis.csv", dependencies=protected)
    def download_analysis(query: Annotated[AnalysisQuery, Query()]):
        return analysis_download(query, "groups")

    @app.get("/v1/exports/analysis-products.csv", dependencies=protected)
    def download_analysis_products(query: Annotated[AnalysisQuery, Query()]):
        return analysis_download(query, "products")

    def analysis_download(query, kind):
        data = selected_analysis(query)
        return Response(
            analysis_csv(data, kind),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="gummy-{query.source}-analysis-{kind}.csv"'
                ),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app
