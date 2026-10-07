from __future__ import annotations

import asyncio
import html
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import xml.etree.ElementTree as ET
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from pydantic import BaseModel, Field

app = FastAPI(title="The Price of a Decade", version="1.0.0")

FEDERAL_BRACKETS = [
    (58_523, 0.14),
    (117_045, 0.205),
    (181_440, 0.26),
    (258_482, 0.29),
    (math.inf, 0.33),
]

PROVINCES: dict[str, dict[str, Any]] = {
    "AB": {"name": "Alberta", "bpa": 22_769, "brackets": [(61_200,.08),(154_259,.10),(185_111,.12),(246_813,.13),(370_220,.14),(math.inf,.15)]},
    "BC": {"name": "British Columbia", "bpa": 13_216, "brackets": [(50_363,.0506),(100_728,.077),(115_648,.105),(140_430,.1229),(190_405,.147),(265_545,.168),(math.inf,.205)]},
    "MB": {"name": "Manitoba", "bpa": 15_780, "brackets": [(47_000,.108),(100_000,.1275),(math.inf,.174)]},
    "NB": {"name": "New Brunswick", "bpa": 13_664, "brackets": [(52_333,.094),(104_666,.14),(193_861,.16),(math.inf,.195)]},
    "NL": {"name": "Newfoundland and Labrador", "bpa": 11_188, "brackets": [(44_678,.087),(89_354,.145),(159_528,.158),(223_340,.178),(285_319,.198),(570_638,.208),(1_141_275,.213),(math.inf,.218)]},
    "NS": {"name": "Nova Scotia", "bpa": 11_932, "brackets": [(30_995,.0879),(61_991,.1495),(97_417,.1667),(157_124,.175),(math.inf,.21)]},
    "NT": {"name": "Northwest Territories", "bpa": 18_198, "brackets": [(53_003,.059),(106_009,.086),(172_346,.122),(math.inf,.1405)]},
    "NU": {"name": "Nunavut", "bpa": 19_659, "brackets": [(55_801,.04),(111_602,.07),(181_439,.09),(math.inf,.115)]},
    "ON": {"name": "Ontario", "bpa": 12_989, "brackets": [(53_891,.0505),(107_785,.0915),(150_000,.1116),(220_000,.1216),(math.inf,.1316)]},
    "PE": {"name": "Prince Edward Island", "bpa": 15_000, "brackets": [(33_928,.095),(65_820,.1347),(106_890,.166),(142_520,.1762),(math.inf,.19)]},
    "QC": {"name": "Quebec", "bpa": 18_952, "brackets": [(54_345,.14),(108_680,.19),(132_245,.24),(math.inf,.2575)]},
    "SK": {"name": "Saskatchewan", "bpa": 20_381, "brackets": [(54_532,.105),(155_805,.125),(math.inf,.145)]},
    "YT": {"name": "Yukon", "bpa": "federal", "brackets": [(58_523,.064),(117_045,.09),(181_440,.109),(500_000,.128),(math.inf,.15)]},
}

CPP = {"ympe": 74_600, "yampe": 85_000, "exemption": 3_500, "base": .0495, "add1": .01, "add2": .04}
QPP = {"ympe": 74_600, "yampe": 85_000, "exemption": 3_500, "base": .053, "add1": .01, "add2": .04}
EI = {"max": 68_900, "rate": .0163}
EI_QC = {"max": 68_900, "rate": .0130}
QPIP = {"max": 103_000, "rate": .00430}
CANADA_EMPLOYMENT_AMOUNT = 1_501

FEEDS = [
    {
        "source": "Statistics Canada",
        "url": "https://www150.statcan.gc.ca/n1/rss/dai-quo/0-eng.atom",
        "home": "https://www.statcan.gc.ca/",
    },
    {
        "source": "Bank of Canada",
        "url": "https://www.bankofcanada.ca/utility/news/feed/",
        "home": "https://www.bankofcanada.ca/",
    },
    {
        "source": "Finance Canada",
        "url": "https://api.io.canada.ca/io-server/gc/news/en/v2?atomtitle=Canada+News+Centre+-+Department+of+Finance+Canada+-+News+Releases&dept=departmentfinance&format=atom&orderBy=desc&pick=100&publishedDate%3E=2020-08-09&sort=publishedDate&type=newsreleases",
        "home": "https://www.canada.ca/en/department-finance.html",
    },
]

_feed_cache: dict[str, Any] = {"at": 0.0, "items": []}

# Equal-share public-cost illustration. Latest finalized CRA Individual Income Tax
# Return Statistics for the 2024 tax year report 31,474,740 returns filed. This
# denominator is an analytical allocation device, not a claim about legal tax incidence.
PUBLIC_COST_DENOMINATOR = 31_474_740
PUBLIC_COST_DENOMINATOR_LABEL = "31,474,740 individual tax returns (CRA, 2024 tax year)"
PUBLIC_COST_DENOMINATOR_SOURCE = "https://www.canada.ca/en/revenue-agency/programs/about-canada-revenue-agency-cra/income-statistics-gst-hst-statistics/t1-final-statistics/2024-tax-year.html"
_spending_cache: dict[str, Any] = {"at": 0.0, "items": [], "meta": {}}

GC_NEWS_RELEASES_FEED = {
    "source": "Government of Canada",
    "url": "https://api.io.canada.ca/io-server/gc/news/en/v2?atomtitle=news+releases&format=atom&orderBy=desc&pick=40&publishedDate%3E=2025-01-01&sort=publishedDate&type=newsreleases",
    "home": "https://www.canada.ca/en/news.html",
}

# Public Money archive: scan the current federal fiscal year (April 1 to March 31)
# from oldest to newest in bounded 100-item pages. The official feed supports
# publishedDate filtering and ordering; advancing by the last returned calendar
# date gives us a practical fiscal-year archive without relying on a short rolling feed.
GC_NEWS_BASE_URL = "https://api.io.canada.ca/io-server/gc/news/en/v2"
PUBLIC_MONEY_ARCHIVE_LIMIT = 500


def _current_fiscal_year_start(today: date | None = None) -> date:
    d = today or datetime.now(timezone.utc).date()
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def _gc_fiscal_feed_source(start_on: date) -> dict[str, str]:
    return {
        "source": "Government of Canada",
        "url": (
            f"{GC_NEWS_BASE_URL}?atomtitle=fiscal+year+news+releases"
            f"&format=atom&orderBy=asc&pick=100"
            f"&publishedDate%3E={start_on.isoformat()}"
            f"&sort=publishedDate&type=newsreleases"
        ),
        "home": "https://www.canada.ca/en/news.html",
    }

_MONEY_RE = re.compile(
    r"(?:(?:C\$|CAD\s*\$?|\$)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(trillion|billion|million|thousand|bn|m|k)?\b)|"
    r"(?:([0-9][0-9,]*(?:\.[0-9]+)?)\s*(trillion|billion|million|thousand|bn|m|k)\s+(?:Canadian\s+)?dollars\b)",
    re.I,
)
_SPENDING_CUES = re.compile(r"\b(announc(?:e|ed|es|ing|ement)?|commit(?:s|ted|ment)?|invest(?:s|ed|ment|ing)?|fund(?:s|ed|ing)?|provide(?:s|d)?|support(?:s|ed|ing)?|contribut(?:e|es|ed|ion)|allocat(?:e|es|ed|ion)|spend(?:s|ing)?|loan|guarantee|assistance|aid|package|grant)\b", re.I)


def progressive_tax(income: float, brackets: list[tuple[float, float]]) -> float:
    total = 0.0
    lower = 0.0
    for upper, rate in brackets:
        if income <= lower:
            break
        taxable = min(income, upper) - lower
        if taxable > 0:
            total += taxable * rate
        lower = upper
    return max(0.0, total)


def federal_bpa(net_income: float) -> float:
    if net_income <= 181_440:
        return 16_452.0
    if net_income >= 258_482:
        return 14_829.0
    return 16_452.0 - (net_income - 181_440.0) * (1_623.0 / 77_042.0)


def manitoba_bpa(net_income: float) -> float:
    if net_income <= 200_000:
        return 15_780.0
    if net_income >= 400_000:
        return 0.0
    return 15_780.0 - (net_income - 200_000.0) * (15_780.0 / 200_000.0)


def pension_contrib(income: float, qc: bool) -> dict[str, float]:
    p = QPP if qc else CPP
    first_band = max(0.0, min(income, p["ympe"]) - p["exemption"])
    base = first_band * p["base"]
    add1 = first_band * p["add1"]
    second_band = max(0.0, min(income, p["yampe"]) - p["ympe"])
    add2 = second_band * p["add2"]
    return {"base": base, "additional": add1 + add2, "total": base + add1 + add2}


def payroll_contrib(income: float, province: str) -> dict[str, float]:
    qc = province == "QC"
    pension = pension_contrib(income, qc)
    ei_def = EI_QC if qc else EI
    ei = min(income, ei_def["max"]) * ei_def["rate"]
    qpip = min(income, QPIP["max"]) * QPIP["rate"] if qc else 0.0
    return {
        "pension_base": pension["base"],
        "pension_additional": pension["additional"],
        "pension_total": pension["total"],
        "ei": ei,
        "qpip": qpip,
        "total": pension["total"] + ei + qpip,
    }


def estimated_income_tax(income: float, province: str) -> dict[str, float]:
    if income <= 0:
        return {"federal": 0.0, "provincial": 0.0, "payroll": 0.0, "total": 0.0, "net": 0.0}

    c = payroll_contrib(income, province)
    # Additional CPP/QPP is deductible in this simplified annual estimate.
    taxable = max(0.0, income - c["pension_additional"])

    fbpa = federal_bpa(taxable)
    federal_basic = progressive_tax(taxable, FEDERAL_BRACKETS)
    federal_credits_base = fbpa + CANADA_EMPLOYMENT_AMOUNT + c["pension_base"] + c["ei"] + c["qpip"]
    federal = max(0.0, federal_basic - .14 * federal_credits_base)

    if province == "QC":
        # Quebec residents receive a 16.5% federal abatement.
        federal *= 0.835

    p = PROVINCES[province]
    bpa = federal_bpa(taxable) if p["bpa"] == "federal" else float(p["bpa"])
    if province == "MB":
        bpa = manitoba_bpa(taxable)
    provincial_basic = progressive_tax(taxable, p["brackets"])
    lowest_rate = p["brackets"][0][1]
    provincial_credit_base = bpa + c["pension_base"] + c["ei"] + c["qpip"]
    provincial = max(0.0, provincial_basic - lowest_rate * provincial_credit_base)

    # Ontario surtax is material at higher incomes; include it. The Ontario Health Premium
    # is intentionally not included in this simplified model and is disclosed in the UI.
    if province == "ON":
        provincial += max(0.0, provincial - 5_818.0) * 0.20
        provincial += max(0.0, provincial_basic - lowest_rate * provincial_credit_base - 7_446.0) * 0.36

    tax_total = federal + provincial
    deductions = tax_total + c["total"]
    net = max(0.0, income - deductions)
    return {
        "federal": federal,
        "provincial": provincial,
        "payroll": c["total"],
        "cpp_qpp": c["pension_total"],
        "ei": c["ei"],
        "qpip": c["qpip"],
        "total": deductions,
        "net": net,
    }


class CalcRequest(BaseModel):
    province: str = Field(pattern="^(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)$")
    mode: str = Field(pattern="^(gross|disposable)$")
    gross_household_income: float = Field(default=0, ge=0, le=100_000_000)
    disposable_household_income: float = Field(default=0, ge=0, le=100_000_000)
    earners: int = Field(default=1, ge=1, le=10)
    hours_per_week: float = Field(default=40, gt=0, le=168)
    weeks_per_year: float = Field(default=50, gt=0, le=52.2)
    household_size: int = Field(default=1, ge=1, le=20)
    housing_status: str = Field(default="homeowner", pattern="^(homeowner|renter|other)$")
    economic_cost: float = Field(default=0, ge=0, le=10_000_000_000)


@app.post("/api/calculate")
def calculate(req: CalcRequest) -> dict[str, Any]:
    annual_productive_hours = req.earners * req.hours_per_week * req.weeks_per_year
    if annual_productive_hours <= 0:
        raise HTTPException(400, "Productive hours must be greater than zero")

    tax_detail: dict[str, float] | None = None
    if req.mode == "disposable":
        disposable = req.disposable_household_income
        gross = req.gross_household_income
    else:
        gross = req.gross_household_income
        per_earner = gross / req.earners
        each = estimated_income_tax(per_earner, req.province)
        disposable = each["net"] * req.earners
        tax_detail = {k: v * req.earners for k, v in each.items() if k != "net"}

    if disposable <= 0:
        raise HTTPException(400, "Disposable income must be greater than zero")

    htv = disposable / annual_productive_hours
    cost_hours = req.economic_cost / htv if req.economic_cost > 0 and htv > 0 else 0.0
    return {
        "province": PROVINCES[req.province]["name"],
        "mode": req.mode,
        "gross_household_income": round(gross, 2),
        "disposable_household_income": round(disposable, 2),
        "annual_productive_hours": round(annual_productive_hours, 2),
        "human_time_value": round(htv, 4),
        "economic_cost": round(req.economic_cost, 2),
        "cost_hours": round(cost_hours, 2),
        "cost_workdays": round(cost_hours / 8.0, 2),
        "cost_workweeks": round(cost_hours / req.hours_per_week, 2),
        "tax_detail": {k: round(v, 2) for k, v in tax_detail.items()} if tax_detail else None,
        "assumption": "Gross household income is divided equally across earners for the automatic tax estimate." if req.mode == "gross" else "Disposable household income was entered directly; no tax estimate was applied.",
        "tax_year": 2026,
        "public_cost_denominator": PUBLIC_COST_DENOMINATOR,
        "public_cost_denominator_label": PUBLIC_COST_DENOMINATOR_LABEL,
        "public_cost_denominator_source": PUBLIC_COST_DENOMINATOR_SOURCE,
    }


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_date(value: str) -> float:
    if not value:
        return 0.0
    candidates = [value.strip()]
    for v in candidates:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(value).timestamp()
    except Exception:
        return 0.0


def parse_feed_xml(content: bytes, source: dict[str, str]) -> list[dict[str, Any]]:
    root = ET.fromstring(content)
    out: list[dict[str, Any]] = []
    nodes = [n for n in root.iter() if _tag_name(n.tag) in {"entry", "item"}]
    for node in nodes[:100]:
        title = "Untitled"
        link = source["home"]
        published = ""
        summary = ""
        author = ""
        for child in list(node):
            name = _tag_name(child.tag)
            # Atom feeds can wrap titles/summaries in nested XHTML.
            text = html.unescape(" ".join(part.strip() for part in child.itertext() if part and part.strip())).strip()
            if name == "title" and text:
                title = text
            elif name == "link":
                href = child.attrib.get("href") or text
                if href and href.startswith("http"):
                    link = href
            elif name in {"published", "updated", "pubdate", "date"} and text and not published:
                published = text
            elif name in {"summary", "description", "content"} and text and not summary:
                summary = text
            elif name in {"author", "creator"} and text and not author:
                author = text
        out.append({
            "source": author or source["source"],
            "title": title,
            "summary": summary,
            "link": link,
            "published": published,
            "timestamp": _parse_date(published),
        })
    return out


async def fetch_feed(source: dict[str, str], client: httpx.AsyncClient) -> list[dict[str, Any]]:
    try:
        r = await client.get(source["url"], timeout=12.0, follow_redirects=True, headers={"User-Agent": "PriceOfADecade/1.0"})
        r.raise_for_status()
        return parse_feed_xml(r.content, source)
    except Exception:
        return []


def _money_value(number: str, unit: str | None) -> float:
    value = float(number.replace(",", ""))
    mult = {
        "trillion": 1_000_000_000_000,
        "billion": 1_000_000_000,
        "bn": 1_000_000_000,
        "million": 1_000_000,
        "m": 1_000_000,
        "thousand": 1_000,
        "k": 1_000,
    }.get((unit or "").lower(), 1)
    return value * mult


def _extract_commitment(text: str) -> tuple[float, str] | None:
    if not text:
        return None
    compact = re.sub(r"\s+", " ", html.unescape(text)).strip()
    # Prefer an amount in a sentence that also describes a government action.
    sentences = re.split(r"(?<=[.!?])\s+", compact)
    candidates: list[tuple[int, int, float, str]] = []
    for sentence_index, sentence in enumerate(sentences):
        cue = bool(_SPENDING_CUES.search(sentence))
        for match_index, m in enumerate(_MONEY_RE.finditer(sentence)):
            number = m.group(1) or m.group(3)
            unit = m.group(2) or m.group(4)
            if not number:
                continue
            amount = _money_value(number, unit)
            if amount < 10_000:
                continue
            # Action-linked amounts rank ahead of incidental amounts. Within a sentence,
            # earlier mentions rank ahead of later ones.
            candidates.append((0 if cue else 1, sentence_index * 100 + match_index, amount, m.group(0).strip()))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    _, _, amount, label = candidates[0]
    return amount, label


def _strip_html_page(content: str) -> str:
    content = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", content)
    content = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", content)
    content = re.sub(r"(?is)<[^>]+>", " ", content)
    return re.sub(r"\s+", " ", html.unescape(content)).strip()


def _commitment_kind(text: str) -> str:
    t = text.lower()
    if "loan guarantee" in t or "guarantee" in t:
        return "Loan guarantee"
    if "repayable" in t or re.search(r"\bloan\b", t):
        return "Loan / repayable support"
    if "aid" in t or "assistance" in t:
        return "Aid / assistance"
    if "invest" in t:
        return "Investment"
    if "grant" in t:
        return "Grant"
    if "fund" in t:
        return "Funding"
    return "Public commitment"




async def fetch_fiscal_year_news(client: httpx.AsyncClient) -> tuple[list[dict[str, Any]], date, date]:
    today = datetime.now(timezone.utc).date()
    fiscal_start = _current_fiscal_year_start(today)
    cursor = fiscal_start
    all_items: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Safety bound: a fiscal year is at most 366 days. With 100 oldest-first
    # releases per request, 60 pages is far beyond the expected requirement but
    # prevents an accidental infinite loop if the upstream feed behaves oddly.
    for _ in range(60):
        if cursor > today:
            break
        page = await fetch_feed(_gc_fiscal_feed_source(cursor), client)
        if not page:
            break

        page.sort(key=lambda x: x.get("timestamp", 0))
        for item in page:
            ts = item.get("timestamp", 0) or 0
            if ts:
                item_date = datetime.fromtimestamp(ts, timezone.utc).date()
                if item_date < fiscal_start or item_date > today:
                    continue
            key = item.get("link") or f"{item.get('title','')}|{item.get('published','')}"
            if key in seen:
                continue
            seen.add(key)
            all_items.append(item)

        valid_ts = [item.get("timestamp", 0) or 0 for item in page if item.get("timestamp", 0)]
        if not valid_ts:
            break
        last_date = datetime.fromtimestamp(max(valid_ts), timezone.utc).date()
        next_cursor = last_date + timedelta(days=1)
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(page) < 100:
            break

    all_items.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return all_items, fiscal_start, today


def _spending_item_from_summary(item: dict[str, Any]) -> dict[str, Any] | None:
    combined = " ".join([item.get("title", ""), item.get("summary", "")])
    found = _extract_commitment(combined)
    if not found:
        return None
    amount, amount_label = found
    return {
        "source": item.get("source") or "Government of Canada",
        "title": item.get("title") or "Government announcement",
        "link": item.get("link"),
        "published": item.get("published", ""),
        "timestamp": item.get("timestamp", 0),
        "amount": round(amount, 2),
        "amount_label": amount_label,
        "kind": _commitment_kind(combined),
    }


async def _spending_item(item: dict[str, Any], client: httpx.AsyncClient) -> dict[str, Any] | None:
    combined = " ".join([item.get("title", ""), item.get("summary", "")])
    found = _extract_commitment(combined)
    page_text = ""
    if not found and item.get("link", "").startswith("http"):
        try:
            r = await client.get(item["link"], timeout=9.0, follow_redirects=True, headers={"User-Agent": "PriceOfADecade/1.0"})
            if r.is_success:
                page_text = _strip_html_page(r.text)
                found = _extract_commitment(page_text[:120_000])
        except Exception:
            pass
    if not found:
        return None
    amount, amount_label = found
    context = combined + " " + page_text[:8_000]
    return {
        "source": item.get("source") or "Government of Canada",
        "title": item.get("title") or "Government announcement",
        "link": item.get("link"),
        "published": item.get("published", ""),
        "timestamp": item.get("timestamp", 0),
        "amount": round(amount, 2),
        "amount_label": amount_label,
        "kind": _commitment_kind(context),
    }


@app.get("/api/spending-feed")
async def spending_feed() -> dict[str, Any]:
    now = time.time()
    if _spending_cache["items"] and now - _spending_cache["at"] < 3600:
        return {
            "items": _spending_cache["items"],
            "cached": True,
            **(_spending_cache.get("meta") or {}),
        }

    async with httpx.AsyncClient() as client:
        fiscal_task = fetch_fiscal_year_news(client)
        latest_task = fetch_feed(GC_NEWS_RELEASES_FEED, client)
        (fiscal_base, fiscal_start, today), latest_base = await asyncio.gather(fiscal_task, latest_task)

        # Fiscal-year archive: use the official feed title + summary so hundreds of
        # releases can be scanned without issuing a page request for every release.
        archive_items = [x for x in (_spending_item_from_summary(item) for item in fiscal_base) if x]

        # Newest releases get full-page enrichment as before, catching commitments
        # whose dollar amount is present on the release page but not in the feed summary.
        enriched_latest = await asyncio.gather(*(_spending_item(item, client) for item in latest_base[:40]))
        latest_items = [x for x in enriched_latest if x]

    merged: dict[tuple[str, float], dict[str, Any]] = {}
    for item in archive_items + latest_items:
        key = ((item.get("link") or item.get("title") or "").strip(), float(item.get("amount", 0) or 0))
        current = merged.get(key)
        if not current or item.get("timestamp", 0) >= current.get("timestamp", 0):
            merged[key] = item

    items = list(merged.values())
    items.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    total_detected = len(items)
    items = items[:PUBLIC_MONEY_ARCHIVE_LIMIT]

    fiscal_end = date(fiscal_start.year + 1, 3, 31)
    meta = {
        "coverage_start": fiscal_start.isoformat(),
        "coverage_end": min(today, fiscal_end).isoformat(),
        "fiscal_year": f"{fiscal_start.year}-{str(fiscal_start.year + 1)[-2:]}",
        "scanned_releases": len(fiscal_base),
        "detected_commitments": total_detected,
        "truncated": total_detected > PUBLIC_MONEY_ARCHIVE_LIMIT,
    }
    _spending_cache["at"] = now
    _spending_cache["items"] = items
    _spending_cache["meta"] = meta
    return {
        "items": items,
        "cached": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **meta,
    }


@app.get("/api/feed")
async def national_feed() -> dict[str, Any]:
    now = time.time()
    if _feed_cache["items"] and now - _feed_cache["at"] < 600:
        return {"items": _feed_cache["items"], "cached": True}
    async with httpx.AsyncClient() as client:
        groups = await asyncio.gather(*(fetch_feed(s, client) for s in FEEDS))
    items = [item for group in groups for item in group]
    items.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    items = items[:18]
    _feed_cache["at"] = now
    _feed_cache["items"] = items
    return {"items": items, "cached": False, "updated_at": datetime.now(timezone.utc).isoformat()}


class ContactRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=200)
    organization: str = Field(default="", max_length=160)
    reason: str = Field(min_length=2, max_length=80)
    subject: str = Field(min_length=2, max_length=180)
    message: str = Field(min_length=10, max_length=5000)
    website: str = Field(default="", max_length=200)


CONTACT_EMAIL = "alain@priceofadecade.com"
CONTACT_DELIVERY_ACTIVE = False
_EMAIL_BASIC_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@app.post("/api/contact")
def contact_submit(req: ContactRequest) -> dict[str, Any]:
    # Honeypot: accept bot submissions without processing them.
    if req.website.strip():
        return {"ok": True, "message": "Thank you."}

    if not _EMAIL_BASIC_RE.match(req.email.strip()):
        raise HTTPException(status_code=422, detail="Please enter a valid email address.")

    if not CONTACT_DELIVERY_ACTIVE:
        raise HTTPException(
            status_code=503,
            detail=(
                f"The contact form is built and validated, but email delivery to {CONTACT_EMAIL} "
                "is not active yet. Please try again once the project mailbox is online."
            ),
        )

    # Delivery provider will be connected here once the project mailbox is operational.
    raise HTTPException(status_code=503, detail="Email delivery is not configured yet.")


@app.get("/static/site.css")
def site_css() -> FileResponse:
    return FileResponse("site.css", media_type="text/css")


@app.get("/static/book-cover.png")
def book_cover_image() -> FileResponse:
    return FileResponse("book-cover.png", media_type="image/png")


@app.get("/static/alain-chicoine.png")
def alain_chicoine_image() -> FileResponse:
    return FileResponse("alain-chicoine.png", media_type="image/png")


@app.get("/static/home-hero-art.jpg")
def home_hero_art_image() -> FileResponse:
    return FileResponse("home-hero-art.jpg", media_type="image/jpeg")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    with open("home.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/contact", response_class=HTMLResponse)
def contact_page() -> HTMLResponse:
    with open("contact.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/calculator", response_class=HTMLResponse)
def calculator_page() -> HTMLResponse:
    with open("calculator.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
