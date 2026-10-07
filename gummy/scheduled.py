from datetime import date, datetime
from zoneinfo import ZoneInfo

from gummy.models import CollectRequest

TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_SEARCH = {
    "demo": ("socket wrench set", "套筒扳手"),
    "rapidapi": ("ratchet wrench", "棘輪扳手(Ratchet Wrench)"),
}


def local_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(TAIPEI)).astimezone(TAIPEI)


def local_day(now: datetime | None = None) -> date:
    return local_now(now).date()


def search_parameters(source: str, query: str | None, category: str | None) -> tuple[str, str]:
    default_query, default_category = DEFAULT_SEARCH[source]
    return query or default_query, category or default_category


def scheduled_request(
    source: str, query: str, category: str, max_products: int, now: datetime | None = None
) -> CollectRequest:
    day = local_day(now)
    return CollectRequest(
        source=source,
        run_key=f"daily-{source}-{day:%Y%m%d}",
        query=query,
        category=category,
        max_products=max_products,
    )


def in_trial_window(day: date, start_date: date | None, end_date: date | None) -> bool:
    return (start_date is None or day >= start_date) and (end_date is None or day <= end_date)
