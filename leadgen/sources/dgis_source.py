from typing import Iterator, List, Optional

import requests

from ..config import settings

DGIS_ENDPOINT = "https://catalog.api.2gis.com/3.0/items"

# Search phrases tuned to surface companies that plausibly need warehouse
# space of their own: wholesale, logistics, fulfilment, manufacturing.
DEFAULT_QUERIES = [
    "оптовая база",
    "логистическая компания",
    "склад ответственного хранения",
    "транспортная компания грузоперевозки",
    "производственная компания",
    "фулфилмент",
    "дистрибьютор",
]

DEFAULT_REGIONS = ["Москва", "Московская область"]


def iter_dgis_companies(
    queries: Optional[List[str]] = None,
    regions: Optional[List[str]] = None,
    pages_per_query: int = 5,
    page_size: int = 20,
) -> Iterator[dict]:
    """Yields dicts with source_id/url/raw_text/company_name/phone/location
    for organizations returned by the official 2GIS Catalog API.

    NOTE: field names below (contact_groups / rubrics / address_name) follow
    the public 2GIS Catalog API docs at https://docs.2gis.com/ - verify against
    your actual API response once DGIS_API_KEY is set, response shape can
    vary slightly by plan/version.
    """
    if not settings.dgis_api_key:
        raise RuntimeError(
            "DGIS_API_KEY не задан в .env. Получить бесплатный ключ: https://dev.2gis.ru/"
        )

    queries = queries or DEFAULT_QUERIES
    regions = regions or DEFAULT_REGIONS

    for region in regions:
        for query in queries:
            q = f"{query} {region}"
            for page in range(1, pages_per_query + 1):
                params = {
                    "q": q,
                    "page": page,
                    "page_size": page_size,
                    "fields": "items.contact_groups,items.rubrics,items.address_name",
                    "key": settings.dgis_api_key,
                }
                try:
                    resp = requests.get(DGIS_ENDPOINT, params=params, timeout=15)
                    resp.raise_for_status()
                    data = resp.json()
                except requests.RequestException as exc:
                    print(f"[2gis] запрос не удался для '{q}' стр. {page}: {exc}")
                    break

                items = (data.get("result") or {}).get("items", [])
                if not items:
                    break

                for item in items:
                    phone = None
                    for group in item.get("contact_groups", []) or []:
                        for contact in group.get("contacts", []) or []:
                            if contact.get("type") == "phone":
                                phone = contact.get("value") or contact.get("text")
                                break
                        if phone:
                            break

                    rubrics = ", ".join(r.get("name", "") for r in item.get("rubrics", []) or [])
                    company_name = item.get("name", "")
                    address = item.get("address_name", "")

                    yield {
                        "source_id": item.get("id") or f"{q}:{page}:{company_name}",
                        "url": f"https://2gis.ru/search/{company_name}",
                        "raw_text": f"{company_name} | рубрика: {rubrics} | адрес: {address}",
                        "company_name": company_name,
                        "phone": phone,
                        "location": address,
                    }

                if len(items) < page_size:
                    break  # последняя страница
