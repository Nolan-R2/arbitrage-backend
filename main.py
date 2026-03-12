"""
ARBITRAGE.AI — Backend API
Run locally: uvicorn main:app --reload --port 8000
Deploy: Railway / Render (see DEPLOY.md)
"""

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import asyncio
import httpx
import feedparser
import re
import os
import json
import time
from datetime import datetime
from typing import Optional
from pydantic import BaseModel

app = FastAPI(title="Arbitrage.AI API")

# Allow requests from your frontend (PWA)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────
# CONFIG — paste your API keys here
# ─────────────────────────────────────────
KEEPA_API_KEY = os.getenv("KEEPA_API_KEY", "YOUR_KEEPA_KEY_HERE")
EBAY_APP_ID   = os.getenv("EBAY_APP_ID",   "YOUR_EBAY_APP_ID_HERE")

# Profit thresholds
MIN_ROI_PERCENT   = 70
AMAZON_FEE_RATE   = 0.15   # 15% referral fee
FBA_FEE_ESTIMATE  = 4.50   # average FBA fulfillment fee
SHIPPING_ESTIMATE = 1.00   # per-item shipping to FBA

# In-memory deal cache (replace with Postgres/Supabase for persistence)
deal_cache = []
last_scan_time = None
scan_in_progress = False

# ─────────────────────────────────────────
# PROFIT CALCULATOR
# ─────────────────────────────────────────
def calculate_roi(buy_price: float, sell_price: float) -> dict:
    fees = (sell_price * AMAZON_FEE_RATE) + FBA_FEE_ESTIMATE + SHIPPING_ESTIMATE
    profit = sell_price - buy_price - fees
    roi = (profit / buy_price) * 100 if buy_price > 0 else 0
    return {
        "profit": round(profit, 2),
        "roi": round(roi, 1),
        "fees": round(fees, 2),
        "passes_filter": roi >= MIN_ROI_PERCENT
    }

# ─────────────────────────────────────────
# DEAL SCRAPERS
# ─────────────────────────────────────────
def extract_price(text: str) -> Optional[float]:
    """Pull the first dollar amount from a string."""
    match = re.search(r'\$([0-9]+(?:\.[0-9]{1,2})?)', text)
    return float(match.group(1)) if match else None

async def scrape_slickdeals() -> list:
    """Fetch hot deals from Slickdeals RSS feed."""
    deals = []
    try:
        feed = feedparser.parse("https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1")
        for entry in feed.entries[:20]:
            price = extract_price(entry.get("title", "") + " " + entry.get("summary", ""))
            if price and price < 500:  # ignore very expensive items
                deals.append({
                    "title": entry.title[:80],
                    "buy_price": price,
                    "source": "Slickdeals",
                    "buy_url": entry.link,
                    "category": "General",
                    "raw_text": entry.get("summary", "")[:200],
                })
    except Exception as e:
        print(f"Slickdeals scrape error: {e}")
    return deals

async def scrape_woot() -> list:
    """Fetch deals from Woot RSS."""
    deals = []
    try:
        feed = feedparser.parse("https://www.woot.com/blog/feed")
        for entry in feed.entries[:10]:
            price = extract_price(entry.get("title", "") + " " + entry.get("summary", ""))
            if price:
                deals.append({
                    "title": entry.title[:80],
                    "buy_price": price,
                    "source": "Woot",
                    "buy_url": entry.link,
                    "category": "Electronics",
                    "raw_text": entry.get("summary", "")[:200],
                })
    except Exception as e:
        print(f"Woot scrape error: {e}")
    return deals

async def scrape_dealnews() -> list:
    """Fetch from DealNews RSS."""
    deals = []
    try:
        feed = feedparser.parse("https://www.dealnews.com/c142/Electronics/?rss=1")
        for entry in feed.entries[:15]:
            price = extract_price(entry.get("title", "") + " " + entry.get("summary", ""))
            if price:
                deals.append({
                    "title": entry.title[:80],
                    "buy_price": price,
                    "source": "DealNews",
                    "buy_url": entry.link,
                    "category": "Electronics",
                    "raw_text": entry.get("summary", "")[:200],
                })
    except Exception as e:
        print(f"DealNews scrape error: {e}")
    return deals

# ─────────────────────────────────────────
# PRICE LOOKUP — KEEPA (Amazon)
# ─────────────────────────────────────────
async def get_amazon_price(title: str) -> Optional[float]:
    """
    Search Keepa for the product and return the current Amazon price.
    Requires a paid Keepa API key (~$20/mo).
    Returns None if key not configured.
    """
    if KEEPA_API_KEY == "YOUR_KEEPA_KEY_HERE":
        # Simulate a price for demo mode (remove when you add real key)
        import random
        return round(float(re.sub(r'[^0-9]', '', title[:3]) or 50) * random.uniform(1.8, 3.5), 2)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.keepa.com/search",
                params={
                    "key": KEEPA_API_KEY,
                    "domain": 1,  # amazon.com
                    "type": "product",
                    "term": title,
                    "range": 90,
                },
                timeout=10
            )
            data = resp.json()
            products = data.get("products", [])
            if products:
                # Get the current Amazon price (in cents)
                price_cents = products[0].get("stats", {}).get("current", [None])[0]
                if price_cents and price_cents > 0:
                    return price_cents / 100
    except Exception as e:
        print(f"Keepa error: {e}")
    return None

# ─────────────────────────────────────────
# PRICE LOOKUP — EBAY (sold listings)
# ─────────────────────────────────────────
async def get_ebay_sold_price(title: str) -> Optional[float]:
    """
    Search eBay completed/sold listings for average price.
    Requires free eBay Developer account + App ID.
    """
    if EBAY_APP_ID == "YOUR_EBAY_APP_ID_HERE":
        return None  # Skip if not configured

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://svcs.ebay.com/services/search/FindingService/v1",
                params={
                    "OPERATION-NAME": "findCompletedItems",
                    "SERVICE-VERSION": "1.0.0",
                    "SECURITY-APPNAME": EBAY_APP_ID,
                    "RESPONSE-DATA-FORMAT": "JSON",
                    "keywords": title[:60],
                    "itemFilter(0).name": "SoldItemsOnly",
                    "itemFilter(0).value": "true",
                    "sortOrder": "EndTimeSoonest",
                    "paginationInput.entriesPerPage": "10",
                },
                timeout=10
            )
            data = resp.json()
            items = (data
                .get("findCompletedItemsResponse", [{}])[0]
                .get("searchResult", [{}])[0]
                .get("item", []))
            if items:
                prices = [
                    float(i["sellingStatus"][0]["currentPrice"][0]["__value__"])
                    for i in items if i.get("sellingStatus")
                ]
                return round(sum(prices) / len(prices), 2) if prices else None
    except Exception as e:
        print(f"eBay error: {e}")
    return None

# ─────────────────────────────────────────
# MAIN SCAN PIPELINE
# ─────────────────────────────────────────
async def run_scan():
    global deal_cache, last_scan_time, scan_in_progress
    scan_in_progress = True
    print(f"[{datetime.now()}] Starting deal scan...")

    # 1. Gather raw deals from all sources
    all_raw = []
    scrapers = [scrape_slickdeals(), scrape_woot(), scrape_dealnews()]
    results = await asyncio.gather(*scrapers, return_exceptions=True)
    for r in results:
        if isinstance(r, list):
            all_raw.extend(r)

    print(f"  Found {len(all_raw)} raw deals")

    # 2. For each deal, look up sell price and calculate ROI
    qualified = []
    for deal in all_raw:
        title = deal["title"]
        buy   = deal["buy_price"]

        # Try Amazon first, fall back to eBay
        sell = await get_amazon_price(title)
        if not sell:
            sell = await get_ebay_sold_price(title)
        if not sell:
            continue  # Can't price it, skip

        calc = calculate_roi(buy, sell)
        if calc["passes_filter"]:
            qualified.append({
                "id": hash(title + str(buy)),
                "title": title,
                "buy_price": buy,
                "sell_price": sell,
                "profit": calc["profit"],
                "roi": calc["roi"],
                "fees": calc["fees"],
                "source": deal["source"],
                "buy_url": deal["buy_url"],
                "category": deal["category"],
                "scanned_at": datetime.now().isoformat(),
            })

    # Sort by ROI descending
    qualified.sort(key=lambda x: x["roi"], reverse=True)
    deal_cache = qualified
    last_scan_time = datetime.now().isoformat()
    scan_in_progress = False
    print(f"  ✅ {len(qualified)} deals passed {MIN_ROI_PERCENT}% ROI filter")

# ─────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "Arbitrage.AI backend running ✅"}

@app.get("/deals")
def get_deals(min_roi: float = 70, category: Optional[str] = None):
    """Return current deals from cache, optionally filtered."""
    results = [d for d in deal_cache if d["roi"] >= min_roi]
    if category and category != "All":
        results = [d for d in results if d["category"] == category]
    return {
        "deals": results,
        "count": len(results),
        "last_scan": last_scan_time,
        "scan_in_progress": scan_in_progress,
    }

@app.post("/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    """Manually trigger a new scan."""
    if scan_in_progress:
        return {"message": "Scan already in progress", "scan_in_progress": True}
    background_tasks.add_task(run_scan)
    return {"message": "Scan started", "scan_in_progress": True}

@app.get("/status")
def status():
    return {
        "deals_in_cache": len(deal_cache),
        "last_scan": last_scan_time,
        "scan_in_progress": scan_in_progress,
        "keepa_configured": KEEPA_API_KEY != "YOUR_KEEPA_KEY_HERE",
        "ebay_configured": EBAY_APP_ID != "YOUR_EBAY_APP_ID_HERE",
    }

# ─────────────────────────────────────────
# AUTO-SCAN on startup + every 2 hours
# ─────────────────────────────────────────
@app.on_event("startup")
async def startup():
    asyncio.create_task(auto_scan_loop())

async def auto_scan_loop():
    await asyncio.sleep(3)  # Small delay after startup
    while True:
        await run_scan()
        await asyncio.sleep(2 * 60 * 60)  # Re-scan every 2 hours
