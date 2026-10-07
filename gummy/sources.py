import json
import re
from copy import deepcopy
from dataclasses import dataclass

import httpx

from gummy.db import utc_now
from gummy.errors import QuotaExceeded, SourceError
from gummy.models import valid_asin

RAPIDAPI_HOST = "real-time-amazon-data.p.rapidapi.com"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PROVIDER_QUOTA_HEADERS = (
    "x-rate-limit-rapid-free-plans-hard-limit-remaining",
    "x-ratelimit-requests-remaining",
)


@dataclass(frozen=True)
class SearchHit:
    asin: str
    sales_volume: str | None = None


# Artificial fixtures: these ASINs, brands and prices are NOT observed Amazon data.
DEMO_PRODUCTS = [
    {
        "asin": "DEMO000001",
        "product_title": "[模擬] 公制鋼製套筒組",
        "product_price": "$10.50",
        "currency": "USD",
        "country": "US",
        "product_star_rating": "4.5",
        "product_num_ratings": 12,
        "product_details": {
            "Brand": "Demo A",
            "Material": "Steel",
            "Measurement System": "Metric",
            "Size": "10 mm",
        },
    },
    {
        "asin": "DEMO000002",
        "product_title": "[模擬] 英制鋼製套筒組",
        "product_price": "20.00",
        "currency": "USD",
        "country": "US",
        "product_star_rating": "4.0",
        "product_num_ratings": 8,
        "product_details": {
            "Brand": "Demo B",
            "Material": "Steel",
            "Measurement System": "SAE",
            "Size": "3/8 inch",
        },
    },
]


class DemoSource:
    def search(self, query: str, country: str) -> list[SearchHit]:
        return [SearchHit(product["asin"]) for product in DEMO_PRODUCTS]

    def details(self, asin: str, country: str) -> dict:
        return deepcopy(next(product for product in DEMO_PRODUCTS if product["asin"] == asin))


class RapidAPISource:
    def __init__(self, settings, db, run_id: int, transport=None):
        settings.validate_live()
        self.settings, self.db, self.run_id = settings, db, run_id
        quota = db.provider_quota_status(settings.rapidapi_key)
        self.provider_remaining: int | None = (
            quota["remaining_estimate"] if quota is not None else None
        )
        self.client = httpx.Client(
            base_url=f"https://{RAPIDAPI_HOST}",
            headers={"X-RapidAPI-Key": settings.rapidapi_key, "X-RapidAPI-Host": RAPIDAPI_HOST},
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self):
        self.client.close()

    def require_provider_capacity(self, planned_requests: int):
        # Keep one provider request in reserve when a quota header is available.
        if self.provider_remaining is not None and self.provider_remaining <= planned_requests:
            raise QuotaExceeded("Provider request quota is too low to finish the batch safely")

    def _get(self, endpoint: str, params: dict) -> dict:
        attempt_id = self.db.reserve_attempt(
            self.run_id,
            endpoint,
            self.settings.total_limit,
            self.settings.daily_limit,
        )
        outcome = "network_error"
        quota_accounted = False
        try:
            # No retries, redirects or pagination: one reservation means at most one HTTP request.
            with self.client.stream("GET", endpoint, params=params) as response:
                outcome = f"http_{response.status_code}"
                if response.status_code != 200:
                    raise SourceError(
                        f"Provider returned HTTP {response.status_code}; no automatic retry"
                    )
                remaining = []
                for name in PROVIDER_QUOTA_HEADERS:
                    value = response.headers.get(name)
                    if value is not None:
                        if re.fullmatch(r"[0-9]{1,9}", value) is None:
                            outcome = "invalid_quota_header"
                            raise SourceError("Provider quota header is invalid; batch stopped")
                        remaining.append(int(value))
                # A later response may omit quota headers; never discard a known
                # estimate or increase it during the same collection batch.
                if self.provider_remaining is not None:
                    self.provider_remaining = max(0, self.provider_remaining - 1)
                if remaining:
                    observed_remaining = min(remaining)
                    self.provider_remaining = (
                        min(self.provider_remaining, observed_remaining)
                        if self.provider_remaining is not None
                        else observed_remaining
                    )
                if self.provider_remaining is not None:
                    self.db.record_provider_quota(
                        self.settings.rapidapi_key, self.provider_remaining, bool(remaining)
                    )
                quota_accounted = True
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        outcome = "response_too_large"
                        raise SourceError("Provider response exceeded the 2 MiB limit")
                    chunks.append(chunk)
                try:
                    payload = json.loads(b"".join(chunks))
                except (ValueError, UnicodeDecodeError):
                    outcome = "invalid_json"
                    raise SourceError("Provider returned invalid JSON") from None
            if not isinstance(payload, dict) or payload.get("status", "OK") != "OK":
                outcome = "invalid_envelope"
                raise SourceError("Provider response was not successful")
            data = payload.get("data")
            if not isinstance(data, dict):
                outcome = "invalid_schema"
                raise SourceError("Provider response has no data object")
            outcome = "success"
            return data
        except httpx.HTTPError:
            raise SourceError("Provider network request failed; no automatic retry") from None
        finally:
            if not quota_accounted and self.provider_remaining is not None:
                self.provider_remaining = max(0, self.provider_remaining - 1)
                self.db.record_provider_quota(
                    self.settings.rapidapi_key, self.provider_remaining, False
                )
            self.db.finish_attempt(attempt_id, outcome)

    def search(self, query: str, country: str) -> list[SearchHit]:
        data = self._get(
            "/search",
            {
                "query": query,
                "country": country,
                "page": "1",
                "sort_by": "RELEVANCE",
            },
        )
        if data.get("country", country) != country:
            raise SourceError("Search returned a different marketplace")
        found = data.get("products")
        if not isinstance(found, list):
            raise SourceError("Search response has no products list")
        hits = []
        seen_asins = set()
        for item in found:
            if not isinstance(item, dict) or not valid_asin(item.get("asin")):
                raise SourceError("Search returned an invalid ASIN")
            if item["asin"] not in seen_asins:
                hint = item.get("sales_volume")
                hits.append(
                    SearchHit(
                        item["asin"],
                        hint.strip()
                        if isinstance(hint, str) and 0 < len(hint.strip()) <= 120
                        else None,
                    )
                )
                seen_asins.add(item["asin"])
        return hits

    def details(self, asin: str, country: str) -> dict:
        if not valid_asin(asin):
            raise SourceError("Invalid ASIN; no request sent")
        return self._get("/product-details", {"asin": asin, "country": country})


def observation_time() -> str:
    return utc_now()
