"""
Cloudflare Inventory API

Endpoints:
    GET /                                             — dashboard HTML
    GET /colors_and_type.css                          — design system CSS
    GET /config                                       — has_server_token flag
    GET /zones?token=                                 — all zones
    GET /zones/{zone_id}/analytics?token=&days=7      — zone traffic
    GET /workers?token=                               — all worker scripts
    GET /workers/{account_id}/{script_name}/analytics?token=&days=7
    GET /health

Run:
    python main.py
"""

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cf-inventory")

logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

CF_API = "https://api.cloudflare.com/client/v4"
CF_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
HOST = "0.0.0.0"

CF_TOKEN = os.getenv("CF_TOKEN")
PORT = int(os.getenv("PORT", "19232"))


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    if not CF_TOKEN:
        log.warning("=" * 60)
        log.warning("CF_TOKEN not set — token must be provided per-request")
        log.warning("To skip the gate, create .env with:")
        log.warning("  CF_TOKEN=your_cloudflare_api_token")
        log.warning("  PORT=19232  # optional")
        log.warning("=" * 60)
    else:
        log.info("=" * 60)
        log.info("Cloudflare Inventory API  —  starting up")
        log.info(f"CF API     : {CF_API}")
        log.info(f"Port       : {PORT}")
        log.info(f"Token      : {CF_TOKEN[:10]}...")
        log.info(f"Dashboard  : http://localhost:{PORT}/")
        log.info("=" * 60)
    yield
    log.info("Cloudflare Inventory API  —  shut down")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Cloudflare Inventory API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    params = {
        k: ("***" if k == "token" else v) for k, v in request.query_params.items()
    }
    log.info(f"→ {request.method} {request.url.path}  params={params}")
    response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000
    log.info(
        f"← {request.method} {request.url.path}  status={response.status_code}  {elapsed:.1f}ms"
    )
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def resolve_token(token: str | None) -> str:
    actual = token or CF_TOKEN
    if not actual:
        log.error("no token provided and CF_TOKEN not set in .env")
        raise HTTPException(
            status_code=400,
            detail="Cloudflare token required. Provide via ?token= or set CF_TOKEN in .env",
        )
    return actual


def make_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def date_range(days: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return since, until


async def cf_get(
    client: httpx.AsyncClient, url: str, headers: dict, label: str = ""
) -> dict:
    tag = label or url
    log.debug(f"[cf_get] {tag}")
    r = await client.get(url, headers=headers)
    log.debug(f"[cf_get] {tag}  HTTP {r.status_code}")
    if r.status_code == 401:
        log.error(f"[cf_get] 401 — {tag}")
        raise HTTPException(
            status_code=401, detail="Invalid Cloudflare token or missing permission"
        )
    if r.status_code == 403:
        log.error(f"[cf_get] 403 — {tag}")
        raise HTTPException(
            status_code=403, detail=f"Token lacks permission for: {tag}"
        )
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        errors = data.get("errors", [])
        log.error(f"[cf_get] API error — {tag}: {errors}")
        raise HTTPException(status_code=502, detail=f"Cloudflare API error: {errors}")
    return data


async def cf_paginate(
    client: httpx.AsyncClient, url: str, headers: dict, label: str = ""
) -> list:
    tag = label or url
    results = []
    page = 1
    log.debug(f"[paginate] START  {tag}")
    while True:
        log.debug(f"[paginate] {tag}  page={page}")
        r = await client.get(
            url, headers=headers, params={"page": page, "per_page": 50}
        )
        log.debug(f"[paginate] {tag}  page={page}  HTTP {r.status_code}")
        if r.status_code in (401, 403):
            log.error(f"[paginate] HTTP {r.status_code} — {tag}")
            raise HTTPException(
                status_code=r.status_code, detail=f"Auth error on {tag}"
            )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            log.error(f"[paginate] API error — {data.get('errors')}")
            break
        items = data.get("result", [])
        results.extend(items)
        log.debug(
            f"[paginate] {tag}  page={page}  got={len(items)}  total={len(results)}"
        )
        info = data.get("result_info", {})
        total_pages = info.get("total_pages", 1)
        if page >= total_pages or not items:
            break
        page += 1
    log.debug(f"[paginate] DONE  {tag}  total={len(results)}")
    return results


async def cf_graphql(
    client: httpx.AsyncClient,
    headers: dict,
    query: str,
    variables: dict,
    label: str = "",
) -> dict:
    tag = label or "graphql"
    log.debug(f"[graphql] {tag}  vars={variables}")
    r = await client.post(
        CF_GRAPHQL, headers=headers, json={"query": query, "variables": variables}
    )
    log.debug(f"[graphql] {tag}  HTTP {r.status_code}")
    if r.status_code == 401:
        raise HTTPException(
            status_code=401, detail="Invalid token for GraphQL analytics"
        )
    r.raise_for_status()
    data = r.json()
    if "errors" in data and data["errors"]:
        log.error(f"[graphql] {tag}  errors: {data['errors']}")
        raise HTTPException(status_code=502, detail=f"GraphQL error: {data['errors']}")
    log.debug(f"[graphql] {tag}  OK")
    return data.get("data", {})


# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------
ZONE_ANALYTICS_QUERY = """
query ZoneAnalytics($zoneTag: string!, $since: Date!, $until: Date!) {
  viewer {
    zones(filter: { zoneTag: $zoneTag }) {
      httpRequests1dGroups(
        limit: 366
        filter: { date_geq: $since, date_leq: $until }
        orderBy: [date_ASC]
      ) {
        dimensions { date }
        sum {
          requests
          bytes
          cachedRequests
          cachedBytes
          threats
          pageViews
        }
        uniq { uniques }
      }
    }
  }
}
"""

WORKER_ANALYTICS_QUERY = """
query WorkerAnalytics($accountTag: string!, $scriptName: string!, $since: DateTime!, $until: DateTime!) {
  viewer {
    accounts(filter: { accountTag: $accountTag }) {
      workersInvocationsAdaptive(
        limit: 10000
        filter: {
          scriptName: $scriptName
          datetime_geq: $since
          datetime_leq: $until
        }
        orderBy: [datetime_ASC]
      ) {
        sum { requests errors subrequests }
        quantiles { cpuTimeP50 cpuTimeP99 }
        dimensions { datetime scriptName status }
      }
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
@app.get("/")
async def dashboard():
    log.debug("[/] serving dashboard")
    return FileResponse("cf-dash.html")


@app.get("/colors_and_type.css")
async def css():
    log.debug("[/colors_and_type.css] serving CSS")
    return FileResponse("colors_and_type.css", media_type="text/css")


# ---------------------------------------------------------------------------
# Config — token gate handshake
# ---------------------------------------------------------------------------
@app.get("/config")
async def config():
    log.debug("[/config] checking token availability")
    return {
        "has_server_token": CF_TOKEN is not None,
        "token_required": CF_TOKEN is None,
    }


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------
@app.get("/zones")
async def get_zones(
    token: str = Query(
        None, description="Cloudflare API token (optional if set in .env)"
    ),
):
    """All zones (domains): status, plan, nameservers, account."""
    t0 = time.perf_counter()
    actual = resolve_token(token)
    log.info("[/zones] START")
    headers = make_headers(actual)

    async with httpx.AsyncClient(timeout=30) as client:
        zones = await cf_paginate(client, f"{CF_API}/zones", headers, label="zones")
        log.info(f"[/zones] found {len(zones)} zones")

        result = [
            {
                "id": z.get("id"),
                "name": z.get("name"),
                "status": z.get("status"),
                "paused": z.get("paused"),
                "type": z.get("type"),
                "plan": z.get("plan", {}).get("name"),
                "account": {
                    "id": z.get("account", {}).get("id"),
                    "name": z.get("account", {}).get("name"),
                },
                "nameservers": z.get("name_servers", []),
                "original_nameservers": z.get("original_name_servers", []),
                "created_on": z.get("created_on"),
                "modified_on": z.get("modified_on"),
                "activated_on": z.get("activated_on"),
                "development_mode": z.get("development_mode"),
                "owner": {
                    "id": z.get("owner", {}).get("id"),
                    "type": z.get("owner", {}).get("type"),
                    "email": z.get("owner", {}).get("email"),
                },
            }
            for z in zones
        ]

        elapsed = (time.perf_counter() - t0) * 1000
        log.info(f"[/zones] DONE  {len(result)} zones  {elapsed:.0f}ms")
        return {"total": len(result), "zones": result}


# ---------------------------------------------------------------------------
# Zone analytics
# ---------------------------------------------------------------------------
@app.get("/zones/{zone_id}/analytics")
async def get_zone_analytics(
    zone_id: str,
    token: str = Query(
        None, description="Cloudflare API token (optional if set in .env)"
    ),
    days: int = Query(7, ge=1, le=365, description="Period in days (1–365)"),
):
    """Traffic analytics for a zone: requests, bytes, cached, threats, uniques per day."""
    actual = resolve_token(token)
    log.info(f"[/zones/analytics] zone={zone_id}  days={days}")
    headers = make_headers(actual)

    since, until = date_range(days)
    since_date = since[:10]
    until_date = until[:10]
    log.debug(f"[/zones/analytics] period  {since_date} → {until_date}")

    async with httpx.AsyncClient(timeout=30) as client:
        data = await cf_graphql(
            client,
            headers,
            query=ZONE_ANALYTICS_QUERY,
            variables={"zoneTag": zone_id, "since": since_date, "until": until_date},
            label=f"zone-analytics/{zone_id}",
        )

    zones_data = data.get("viewer", {}).get("zones", [])
    if not zones_data:
        log.warning(f"[/zones/analytics] no data for zone {zone_id}")
        return {
            "zone_id": zone_id,
            "days": days,
            "period": {"since": since_date, "until": until_date},
            "daily": [],
            "totals": {},
        }

    groups = zones_data[0].get("httpRequests1dGroups", [])
    log.info(f"[/zones/analytics] {len(groups)} daily buckets")

    daily = []
    totals = {
        "requests": 0,
        "bytes": 0,
        "cached_requests": 0,
        "cached_bytes": 0,
        "threats": 0,
        "page_views": 0,
        "uniques": 0,
    }

    for g in groups:
        s = g.get("sum", {})
        u = g.get("uniq", {})
        row = {
            "date": g.get("dimensions", {}).get("date", ""),
            "requests": s.get("requests", 0),
            "bytes": s.get("bytes", 0),
            "cached_requests": s.get("cachedRequests", 0),
            "cached_bytes": s.get("cachedBytes", 0),
            "threats": s.get("threats", 0),
            "page_views": s.get("pageViews", 0),
            "uniques": u.get("uniques", 0),
        }
        daily.append(row)
        for k in totals:
            totals[k] += row.get(k, 0)

    log.info(f"[/zones/analytics] totals: {totals}")
    return {
        "zone_id": zone_id,
        "days": days,
        "period": {"since": since_date, "until": until_date},
        "daily": daily,
        "totals": totals,
    }


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------
@app.get("/workers")
async def get_workers(
    token: str = Query(
        None, description="Cloudflare API token (optional if set in .env)"
    ),
):
    """All worker scripts across all accounts: routes, crons, active flag."""
    t0 = time.perf_counter()
    actual = resolve_token(token)
    log.info("[/workers] START")
    headers = make_headers(actual)

    async with httpx.AsyncClient(timeout=60) as client:
        log.info("[/workers] fetching accounts")
        accounts_data = await cf_get(
            client, f"{CF_API}/accounts", headers, label="accounts"
        )
        accounts = accounts_data.get("result", [])
        log.info(
            f"[/workers] {len(accounts)} accounts: {[a['name'] for a in accounts]}"
        )

        all_workers = []

        for account in accounts:
            acct_id = account["id"]
            acct_name = account["name"]
            log.info(f"[/workers] account={acct_name} ({acct_id})")

            try:
                scripts_data = await cf_get(
                    client,
                    f"{CF_API}/accounts/{acct_id}/workers/scripts",
                    headers,
                    label=f"scripts/{acct_name}",
                )
                scripts = scripts_data.get("result", [])
                log.info(f"[/workers] account={acct_name}  scripts={len(scripts)}")
            except HTTPException as e:
                log.warning(f"[/workers] skipping {acct_name}: {e.detail}")
                continue

            for script in scripts:
                name = (
                    script.get("id")
                    or script.get("script_name")
                    or script.get("name", "")
                )
                log.debug(f"[/workers] script={name}")

                routes = []
                try:
                    rd = await cf_get(
                        client,
                        f"{CF_API}/accounts/{acct_id}/workers/scripts/{name}/routes",
                        headers,
                        label=f"routes/{name}",
                    )
                    routes = rd.get("result", [])
                    log.debug(f"[/workers] {name}  routes={len(routes)}")
                except Exception as e:
                    log.warning(f"[/workers] routes failed for {name}: {e}")

                crons = []
                try:
                    cd = await cf_get(
                        client,
                        f"{CF_API}/accounts/{acct_id}/workers/scripts/{name}/schedules",
                        headers,
                        label=f"schedules/{name}",
                    )
                    crons = cd.get("result", {}).get("schedules", [])
                    log.debug(f"[/workers] {name}  crons={len(crons)}")
                except Exception as e:
                    log.warning(f"[/workers] schedules failed for {name}: {e}")

                all_workers.append(
                    {
                        "account_id": acct_id,
                        "account_name": acct_name,
                        "name": name,
                        "etag": script.get("etag"),
                        "usage_model": script.get("usage_model"),
                        "created_on": script.get("created_on"),
                        "modified_on": script.get("modified_on"),
                        "routes": [
                            {
                                "pattern": r.get("pattern"),
                                "zone_id": r.get("zone_id"),
                                "zone_name": r.get("zone_name"),
                            }
                            for r in routes
                        ],
                        "cron_triggers": [c.get("cron") for c in crons],
                        "active": bool(routes or crons),
                    }
                )

        all_workers.sort(key=lambda w: w["modified_on"] or "", reverse=True)
        active = sum(1 for w in all_workers if w["active"])
        inactive = len(all_workers) - active
        elapsed = (time.perf_counter() - t0) * 1000
        log.info(
            f"[/workers] DONE  total={len(all_workers)}  active={active}  inactive={inactive}  {elapsed:.0f}ms"
        )

        return {
            "total": len(all_workers),
            "active": active,
            "inactive": inactive,
            "workers": all_workers,
        }


# ---------------------------------------------------------------------------
# Worker analytics
# ---------------------------------------------------------------------------
@app.get("/workers/{account_id}/{script_name}/analytics")
async def get_worker_analytics(
    account_id: str,
    script_name: str,
    token: str = Query(
        None, description="Cloudflare API token (optional if set in .env)"
    ),
    days: int = Query(7, ge=1, le=90, description="Period in days (1–90)"),
):
    """Worker invocations, errors, CPU time per bucket."""
    actual = resolve_token(token)
    log.info(
        f"[/workers/analytics] account={account_id}  script={script_name}  days={days}"
    )
    headers = make_headers(actual)
    since, until = date_range(days)
    log.debug(f"[/workers/analytics] period  {since} → {until}")

    async with httpx.AsyncClient(timeout=30) as client:
        data = await cf_graphql(
            client,
            headers,
            query=WORKER_ANALYTICS_QUERY,
            variables={
                "accountTag": account_id,
                "scriptName": script_name,
                "since": since,
                "until": until,
            },
            label=f"worker-analytics/{script_name}",
        )

    accounts_data = data.get("viewer", {}).get("accounts", [])
    if not accounts_data:
        log.warning(f"[/workers/analytics] no data for {script_name}")
        return {
            "account_id": account_id,
            "script_name": script_name,
            "days": days,
            "period": {"since": since, "until": until},
            "buckets": [],
            "totals": {},
        }

    invocations = accounts_data[0].get("workersInvocationsAdaptive", [])
    log.info(f"[/workers/analytics] {len(invocations)} buckets")

    buckets = []
    totals = {"requests": 0, "errors": 0, "subrequests": 0}

    for inv in invocations:
        s = inv.get("sum", {})
        q = inv.get("quantiles", {})
        d = inv.get("dimensions", {})
        row = {
            "datetime": d.get("datetime"),
            "status": d.get("status"),
            "requests": s.get("requests", 0),
            "errors": s.get("errors", 0),
            "subrequests": s.get("subrequests", 0),
            "cpu_time_p50_us": q.get("cpuTimeP50"),
            "cpu_time_p99_us": q.get("cpuTimeP99"),
        }
        buckets.append(row)
        totals["requests"] += row["requests"]
        totals["errors"] += row["errors"]
        totals["subrequests"] += row["subrequests"]

    totals["error_rate_pct"] = (
        round(totals["errors"] / totals["requests"] * 100, 2)
        if totals["requests"]
        else 0.0
    )
    log.info(f"[/workers/analytics] totals: {totals}")

    return {
        "account_id": account_id,
        "script_name": script_name,
        "days": days,
        "period": {"since": since, "until": until},
        "buckets": buckets,
        "totals": totals,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Create API Token via Global API Key
# ---------------------------------------------------------------------------
@app.post("/create-token")
async def create_token(
    email: str = Query(..., description="Cloudflare account email"),
    global_key: str = Query(..., description="Global API Key"),
    name: str = Query("cf-inventory-token", description="Name for the new token"),
):
    log.info(f"[/create-token] START  email={email}  name={name}")

    headers = {
        "X-Auth-Email": email,
        "X-Auth-Key": global_key,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Verify credentials
        r = await client.get(f"{CF_API}/user", headers=headers)
        if r.status_code == 403:
            raise HTTPException(
                status_code=403, detail="Invalid email or Global API Key"
            )
        r.raise_for_status()
        user = r.json().get("result", {})
        log.info(f"[/create-token] authenticated as {user.get('email')}")

        # 2. Get account id
        ra = await client.get(
            f"{CF_API}/accounts", headers=headers, params={"per_page": 1}
        )
        ra.raise_for_status()
        accounts = ra.json().get("result", [])
        if not accounts:
            raise HTTPException(status_code=404, detail="No Cloudflare accounts found")
        account_id = accounts[0]["id"]
        account_name = accounts[0]["name"]
        log.info(f"[/create-token] account  {account_name}  ({account_id})")

        # 3. Fetch actual permission group IDs from CF
        rp = await client.get(
            f"{CF_API}/user/tokens/permission_groups", headers=headers
        )
        rp.raise_for_status()
        all_groups = rp.json().get("result", [])
        log.info(f"[/create-token] fetched {len(all_groups)} permission groups")

        groups = {g["name"]: g["id"] for g in all_groups}
        log.debug(f"[/create-token] available groups: {list(groups.keys())}")

        def get_group(name: str) -> dict:
            gid = groups.get(name)
            if not gid:
                log.error(f"[/create-token] permission group not found: '{name}'")
                raise HTTPException(
                    status_code=500, detail=f"Permission group not found: '{name}'"
                )
            return {"id": gid, "name": name}

        # 4. Build policies using resolved IDs
        # 4. Build policies using resolved IDs
        payload = {
            "name": name,
            "policies": [
                {
                    "effect": "allow",
                    "resources": {"com.cloudflare.api.account.zone.*": "*"},
                    "permission_groups": [
                        get_group("Zone Read"),
                        get_group("Analytics Read"),  # ← было "Zone Analytics Read"
                    ],
                },
                {
                    "effect": "allow",
                    "resources": {f"com.cloudflare.api.account.{account_id}": "*"},
                    "permission_groups": [
                        get_group("Account Settings Read"),
                        get_group("Workers Scripts Read"),
                        get_group("Workers AI Read"),
                    ],
                },
            ],
        }

        # 5. Create the token
        rt = await client.post(f"{CF_API}/user/tokens", headers=headers, json=payload)
        data = rt.json()
        if not data.get("success"):
            errors = data.get("errors", [])
            log.error(f"[/create-token] token creation failed: {errors}")
            raise HTTPException(
                status_code=502, detail=f"Token creation failed: {errors}"
            )

        result = data.get("result", {})
        token_value = result.get("value")
        token_id = result.get("id")
        log.info(f"[/create-token] DONE  token_id={token_id}  name={name}")

        return {
            "success": True,
            "token": token_value,
            "token_id": token_id,
            "token_name": name,
            "account_id": account_id,
            "account_name": account_name,
            "hint": f"Add to .env:  CF_TOKEN={token_value}",
        }


@app.get("/debug/permission-groups")
async def debug_permission_groups(
    email: str = Query(...),
    global_key: str = Query(...),
):
    headers = {
        "X-Auth-Email": email,
        "X-Auth-Key": global_key,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        rp = await client.get(
            f"{CF_API}/user/tokens/permission_groups", headers=headers
        )
        rp.raise_for_status()
        groups = rp.json().get("result", [])
    return {
        "total": len(groups),
        "groups": sorted(
            [{"id": g["id"], "name": g["name"]} for g in groups],
            key=lambda x: x["name"],
        ),
    }


# ---------------------------------------------------------------------------
# AI — модели и стоимость запроса
# ---------------------------------------------------------------------------

# Neurons per M tokens/units — из официальной документации CF
# Обновлено: май 2026
AI_PRICING = {
    # LLM
    "@cf/meta/llama-3.1-8b-instruct": {
        "input": 25608,
        "output": 75147,
        "unit": "tokens",
        "category": "LLM",
    },
    "@cf/meta/llama-3.1-8b-instruct-fp8": {
        "input": 15200,
        "output": 37500,
        "unit": "tokens",
        "category": "LLM",
    },
    "@cf/meta/llama-3.1-70b-instruct-fp8-fast": {
        "input": 26668,
        "output": 204805,
        "unit": "tokens",
        "category": "LLM",
    },
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast": {
        "input": 26668,
        "output": 204805,
        "unit": "tokens",
        "category": "LLM",
    },
    "@cf/meta/llama-4-scout-17b-16e-instruct": {
        "input": 21000,
        "output": 85000,
        "unit": "tokens",
        "category": "LLM",
    },
    "@cf/mistral/mistral-7b-instruct-v0.1": {
        "input": 10000,
        "output": 17300,
        "unit": "tokens",
        "category": "LLM",
    },
    "@cf/mistralai/mistral-small-3.1-24b-instruct": {
        "input": 31876,
        "output": 50488,
        "unit": "tokens",
        "category": "LLM",
    },
    "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b": {
        "input": 45170,
        "output": 443756,
        "unit": "tokens",
        "category": "LLM",
    },
    "@cf/google/gemma-4-26b-a4b-it": {
        "input": 20000,
        "output": 60000,
        "unit": "tokens",
        "category": "LLM",
    },
    "@cf/zai-org/glm-4.7-flash": {
        "input": 10000,
        "output": 20000,
        "unit": "tokens",
        "category": "LLM",
    },
    "@cf/moonshotai/kimi-k2.6": {
        "input": 50000,
        "output": 500000,
        "unit": "tokens",
        "category": "LLM",
    },
    # Embeddings
    "@cf/baai/bge-m3": {
        "input": 1300,
        "output": None,
        "unit": "tokens",
        "category": "Embeddings",
    },
    "@cf/google/embeddinggemma-300m": {
        "input": 1300,
        "output": None,
        "unit": "tokens",
        "category": "Embeddings",
    },
    "@cf/baai/bge-base-en-v1.5": {
        "input": 1300,
        "output": None,
        "unit": "tokens",
        "category": "Embeddings",
    },
    "@cf/baai/bge-large-en-v1.5": {
        "input": 1300,
        "output": None,
        "unit": "tokens",
        "category": "Embeddings",
    },
    # STT
    "@cf/deepgram/nova-3": {
        "input": 300,
        "output": None,
        "unit": "seconds",
        "category": "STT",
    },
    "@cf/openai/whisper": {
        "input": 300,
        "output": None,
        "unit": "seconds",
        "category": "STT",
    },
    # Image gen
    "@cf/black-forest-labs/flux-1-schnell": {
        "input": 5700,
        "output": None,
        "unit": "steps",
        "category": "ImageGen",
    },
    "@cf/stabilityai/stable-diffusion-xl-base-1.0": {
        "input": 10000,
        "output": None,
        "unit": "steps",
        "category": "ImageGen",
    },
    "@cf/leonardo/phoenix-1.0": {
        "input": 15000,
        "output": None,
        "unit": "steps",
        "category": "ImageGen",
    },
    "@cf/leonardo/lucid-origin": {
        "input": 15000,
        "output": None,
        "unit": "steps",
        "category": "ImageGen",
    },
}

NEURONS_PER_DAY_FREE = 10_000
NEURONS_PRICE_USD = 0.011 / 1000  # per neuron


def estimate_cost(model_id: str, input_units: int, output_units: int = 0) -> dict:
    """Calculate neurons and USD cost for a request."""
    pricing = AI_PRICING.get(model_id)
    if not pricing:
        return {"neurons": None, "usd": None, "note": "pricing unknown for this model"}

    per_m_in = pricing["input"]
    per_m_out = pricing.get("output") or 0
    unit = pricing["unit"]

    neurons_in = round(input_units * per_m_in / 1_000_000)
    neurons_out = round(output_units * per_m_out / 1_000_000)
    total_neurons = neurons_in + neurons_out
    usd = round(total_neurons * NEURONS_PRICE_USD, 6)

    free_remaining_after = max(0, NEURONS_PER_DAY_FREE - total_neurons)

    return {
        "neurons_input": neurons_in,
        "neurons_output": neurons_out,
        "neurons_total": total_neurons,
        "usd": usd,
        "unit": unit,
        "free_daily_limit": NEURONS_PER_DAY_FREE,
        "pct_of_free_daily": round(total_neurons / NEURONS_PER_DAY_FREE * 100, 2),
    }


@app.get("/ai/models")
async def get_ai_models(
    token: str = Query(
        None, description="Cloudflare API token (optional if set in .env)"
    ),
    account_id: str = Query(
        None, description="Cloudflare account ID (fetched automatically if not set)"
    ),
):
    """
    List Workers AI models with pricing data.
    Fetches live catalog from CF API (accounts/{id}/ai/models/search),
    enriches with local pricing where available.
    """
    actual = resolve_token(token)
    log.info("[/ai/models] START")
    headers = make_headers(actual)

    async with httpx.AsyncClient(timeout=30) as client:
        # Resolve account_id if not provided
        if not account_id:
            log.info("[/ai/models] fetching account_id")
            ra = await cf_get(client, f"{CF_API}/accounts", headers, label="accounts")
            accounts = ra.get("result", [])
            if not accounts:
                raise HTTPException(status_code=404, detail="No accounts found")
            account_id = accounts[0]["id"]
            log.info(f"[/ai/models] account_id={account_id}")

        # Fetch live model catalog from CF
        log.info(f"[/ai/models] fetching models from CF  account={account_id}")
        models = await cf_paginate(
            client,
            f"{CF_API}/accounts/{account_id}/ai/models/search",
            headers,
            label="ai/models/search",
        )
        log.info(f"[/ai/models] CF returned {len(models)} models")

    # If CF returned nothing (token lacks Workers AI Read) — fall back to AI_PRICING
    if not models:
        log.warning(
            "[/ai/models] CF returned empty list — falling back to local AI_PRICING catalog"
        )
        result = [
            {
                "id": model_id,
                "name": model_id,
                "description": None,
                "task": p["category"],
                "tags": [],
                "source": "local",
                "category": p["category"],
                "pricing": {
                    "unit": p["unit"],
                    "input_neurons_per_m": p["input"],
                    "output_neurons_per_m": p.get("output"),
                },
            }
            for model_id, p in AI_PRICING.items()
        ]
    else:
        result = []
        for m in models:
            model_id = m.get("name", "")
            pricing = AI_PRICING.get(model_id)
            result.append(
                {
                    "id": model_id,
                    "name": model_id,
                    "description": m.get("description"),
                    "task": m.get("task", {}).get("name") if m.get("task") else None,
                    "tags": m.get("tags", []),
                    "source": m.get("source", "hosted"),  # hosted | proxied
                    "category": pricing["category"] if pricing else None,
                    "pricing": {
                        "unit": pricing["unit"] if pricing else None,
                        "input_neurons_per_m": pricing["input"] if pricing else None,
                        "output_neurons_per_m": pricing.get("output")
                        if pricing
                        else None,
                    }
                    if pricing
                    else None,
                }
            )

    categories = sorted({r["category"] for r in result if r["category"]})
    with_pricing = sum(1 for r in result if r["pricing"])
    log.info(
        f"[/ai/models] DONE  total={len(result)}  with_pricing={with_pricing}  categories={categories}"
    )
    return {
        "account_id": account_id,
        "total": len(result),
        "categories": categories,
        "models": result,
    }


@app.post("/ai/run")
async def run_ai_model(
    token: str = Query(None),
    model: str = Query(...),
    prompt: str = Query(...),
    system: str = Query(None),
    max_tokens: int = Query(256, ge=1, le=4096),
):
    actual = resolve_token(token)
    log.info(
        f"[/ai/run] model={model}  max_tokens={max_tokens}  prompt_len={len(prompt)}"
    )
    headers = make_headers(actual)

    async with httpx.AsyncClient(timeout=60) as client:
        ra = await cf_get(client, f"{CF_API}/accounts", headers, label="accounts")
        accounts = ra.get("result", [])
        if not accounts:
            raise HTTPException(status_code=404, detail="No accounts found")
        account_id = accounts[0]["id"]

        # Определяем категорию модели
        pricing = AI_PRICING.get(model, {})
        category = pricing.get("category", "LLM")

        # Payload зависит от типа модели
        if category == "ImageGen":
            payload = {"prompt": prompt}
        elif category == "Embeddings":
            payload = {"text": prompt}
        else:
            # LLM — messages format
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            payload = {"messages": messages, "max_tokens": max_tokens}

        log.info(
            f"[/ai/run] category={category}  POST accounts/{account_id}/ai/run/{model}"
        )
        t0 = time.perf_counter()
        r = await client.post(
            f"{CF_API}/accounts/{account_id}/ai/run/{model}",
            headers=headers,
            json=payload,
            timeout=60,
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        log.debug(f"[/ai/run] HTTP {r.status_code}  {elapsed_ms}ms")

        if r.status_code == 400:
            log.error(f"[/ai/run] 400: {r.text}")
            raise HTTPException(
                status_code=400, detail=f"Model rejected request: {r.text}"
            )
        if r.status_code == 403:
            raise HTTPException(
                status_code=403, detail="Token lacks Workers AI permission"
            )
        r.raise_for_status()

        # Image models возвращают бинарный PNG, не JSON
        if category == "ImageGen":
            import base64

            content_type = r.headers.get("content-type", "")

            # RAW PNG/JPEG
            if "image/" in content_type:
                image_b64 = base64.b64encode(r.content).decode()
                image_bytes = len(r.content)

            # JSON response
            else:
                data = r.json()

                if not data.get("success"):
                    errors = data.get("errors", [])
                    raise HTTPException(
                        status_code=502, detail=f"AI run failed: {errors}"
                    )

                result = data.get("result", {})

                image_b64 = (
                    result.get("image")
                    or result.get("b64_json")
                    or result.get("output")
                )

                if not image_b64:
                    raise HTTPException(
                        status_code=502, detail=f"Image not found in response: {result}"
                    )

                image_bytes = len(base64.b64decode(image_b64))

            cost = estimate_cost(model, 1, 0)

            return {
                "model": model,
                "category": category,
                "image_b64": image_b64,
                "image_bytes": image_bytes,
                "latency_ms": elapsed_ms,
                "cost": cost,
            }
            # LLM / Embeddings response
        else:
            data = r.json()

            if not data.get("success"):
                errors = data.get("errors", [])
                raise HTTPException(status_code=502, detail=f"AI run failed: {errors}")

            result = data.get("result", {})

            if category == "Embeddings":
                response_text = None
                input_tokens = len(prompt.split())
                output_tokens = 0
            else:
                response_text = result.get("response", "")
                input_tokens = result.get("usage", {}).get(
                    "prompt_tokens", len(prompt.split())
                )
                output_tokens = result.get("usage", {}).get(
                    "completion_tokens", len((response_text or "").split())
                )

            cost = estimate_cost(model, input_tokens, output_tokens)

            return {
                "model": model,
                "category": category,
                "response": response_text,
                "embeddings": result.get("data") if category == "Embeddings" else None,
                "tokens": {
                    "input_estimated": input_tokens,
                    "output_estimated": output_tokens,
                },
                "latency_ms": elapsed_ms,
                "cost": cost,
            }


@app.get("/health")
async def health():
    log.debug("[/health] ping")
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="warning",
    )
