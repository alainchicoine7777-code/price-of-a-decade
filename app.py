from __future__ import annotations

import asyncio
import html
import math
import time
from datetime import datetime, timezone
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
    for node in nodes[:8]:
        title = "Untitled"
        link = source["home"]
        published = ""
        for child in list(node):
            name = _tag_name(child.tag)
            # Some Atom feeds (including Statistics Canada) wrap titles in nested XHTML.
            # itertext() preserves those titles instead of returning an empty child.text.
            text = html.unescape(" ".join(part.strip() for part in child.itertext() if part and part.strip())).strip()
            if name == "title" and text:
                title = text
            elif name == "link":
                href = child.attrib.get("href") or text
                if href and href.startswith("http"):
                    link = href
            elif name in {"published", "updated", "pubdate", "date"} and text and not published:
                published = text
        out.append({
            "source": source["source"],
            "title": title,
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


@app.get("/static/site.css")
def site_css() -> FileResponse:
    return FileResponse("site.css", media_type="text/css")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    with open("home.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/calculator", response_class=HTMLResponse)
def calculator_page() -> HTMLResponse:
    with open("calculator.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
