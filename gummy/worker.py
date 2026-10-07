from gummy.errors import GummyError, SourceError
from gummy.locking import process_lock
from gummy.models import CollectRequest, normalize_product, source_evidence
from gummy.sources import DemoSource, RapidAPISource, observation_time


def collect(db, settings, request: CollectRequest, transport=None, progress=None) -> dict:
    with process_lock(db):
        return _collect(db, settings, request, transport, progress)


def _collect(db, settings, request, transport, progress):
    if request.source == "rapidapi":
        settings.validate_live()
    run_id, repeated = db.begin_run(request)
    if repeated:
        return {"run_id": run_id, "source": request.source, "status": "SUCCEEDED", "reused": True}
    source = None
    try:
        if progress:
            progress("search", run_id=run_id)
        if request.source == "rapidapi":
            db.preflight_quota(1 + request.max_products, settings.total_limit,
                               settings.daily_limit, run_id=run_id)
            source = RapidAPISource(settings, db, run_id, transport=transport)
            source.require_provider_capacity(1 + request.max_products)
        else:
            source = DemoSource()
        hits = source.search(request.query, request.country)[: request.max_products]
        if not hits:
            raise SourceError("No products returned; previous report remains available")
        items = []
        evidence = []
        for index, hit in enumerate(hits):
            if progress:
                progress("details", run_id=run_id, completed=index, total=len(hits))
            asin = hit.asin
            if isinstance(source, RapidAPISource):
                source.require_provider_capacity(len(hits) - index)
            raw = source.details(asin, request.country)
            items.append(
                {
                    **normalize_product(raw, asin, request.country),
                    "observed_at": observation_time(),
                }
            )
            fields = source_evidence(raw)
            if hit.sales_volume is not None:
                fields["search_sales_volume"] = hit.sales_volume
            evidence.append({"asin": asin, "fields": fields})
        if progress:
            progress("verify", run_id=run_id, completed=len(items), total=len(items))
        db.publish(run_id, items, evidence)
        return {
            "run_id": run_id,
            "source": request.source,
            "status": "SUCCEEDED",
            "reused": False,
            "product_count": len(items),
            "is_demo": request.source == "demo",
        }
    except Exception as exc:
        db.fail(run_id, type(exc).__name__ if isinstance(exc, GummyError) else "internal_error")
        raise
    finally:
        if isinstance(source, RapidAPISource):
            source.close()
