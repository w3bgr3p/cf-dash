"""
Cloudflare Inventory API
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

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cf-inventory")

_diag_log = logging.getLogger("cf-inventory.diag")
_diag_handler = logging.FileHandler("cf-diag.log", encoding="utf-8")
_diag_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s\n%(message)s\n" + "-" * 80, datefmt="%Y-%m-%d %H:%M:%S"
    )
)
_diag_log.addHandler(_diag_handler)
_diag_log.setLevel(logging.DEBUG)
_diag_log.propagate = False


def diag(label: str, request_data: dict, response_data: dict, note: str = "") -> None:
    import json as _json

    msg = (
        f"[DIAG] {label}"
        + (f"  note={note}" if note else "")
        + f"\nREQUEST:\n{_json.dumps(request_data, ensure_ascii=False, indent=2)}"
        + f"\nRESPONSE:\n{_json.dumps(response_data, ensure_ascii=False, indent=2)}"
    )
    _diag_log.debug(msg)


logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

CF_API = "https://api.cloudflare.com/client/v4"
CF_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
HOST = "0.0.0.0"

CF_TOKEN = os.getenv("CF_TOKEN")
PORT = int(os.getenv("PORT", "19232"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not CF_TOKEN:
        log.warning("=" * 60)
        log.warning("CF_TOKEN not set — token must be provided per-request")
        log.warning("=" * 60)
    else:
        log.info("=" * 60)
        log.info("Cloudflare Inventory API  —  starting up")
        log.info(f"Port       : {PORT}")
        log.info(f"Token      : {CF_TOKEN[:10]}...")
        log.info(f"Dashboard  : http://localhost:{PORT}/")
        log.info("=" * 60)
    yield
    log.info("Cloudflare Inventory API  —  shut down")


app = FastAPI(
    title="Cloudflare Inventory API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


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


def resolve_token(token: str | None) -> str:
    actual = token or CF_TOKEN
    if not actual:
        raise HTTPException(
            status_code=400,
            detail="Cloudflare token required. Provide via ?token= or set CF_TOKEN in .env",
        )
    return actual


def make_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


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
        raise HTTPException(
            status_code=401, detail="Invalid Cloudflare token or missing permission"
        )
    if r.status_code == 403:
        raise HTTPException(
            status_code=403, detail=f"Token lacks permission for: {tag}"
        )
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        errors = data.get("errors", [])
        diag(tag, {"url": url}, data, note="api-not-success")
        raise HTTPException(status_code=502, detail=f"Cloudflare API error: {errors}")
    return data


async def cf_paginate(
    client: httpx.AsyncClient, url: str, headers: dict, label: str = ""
) -> list:
    tag = label or url
    results = []
    page = 1
    while True:
        r = await client.get(
            url, headers=headers, params={"page": page, "per_page": 50}
        )
        if r.status_code in (401, 403):
            raise HTTPException(
                status_code=r.status_code, detail=f"Auth error on {tag}"
            )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            break
        items = data.get("result", [])
        results.extend(items)
        info = data.get("result_info", {})
        total_pages = info.get("total_pages", 1)
        if page >= total_pages or not items:
            break
        page += 1
    return results


async def cf_graphql(
    client: httpx.AsyncClient,
    headers: dict,
    query: str,
    variables: dict,
    label: str = "",
) -> dict:
    tag = label or "graphql"
    r = await client.post(
        CF_GRAPHQL, headers=headers, json={"query": query, "variables": variables}
    )
    if r.status_code == 401:
        diag(
            tag,
            {"query": query, "variables": variables},
            {"status": r.status_code, "body": r.text},
            note="401",
        )
        raise HTTPException(
            status_code=401, detail="Invalid token for GraphQL analytics"
        )
    r.raise_for_status()
    data = r.json()
    if "errors" in data and data["errors"]:
        diag(tag, {"query": query, "variables": variables}, data, note="graphql-errors")
        raise HTTPException(status_code=502, detail=f"GraphQL error: {data['errors']}")
    viewer = data.get("data", {}).get("viewer", {})
    zones = viewer.get("zones", [])
    accounts = viewer.get("accounts", [])
    if (zones and not zones[0].get("httpRequests1dGroups")) or (
        accounts and not accounts[0].get("workersInvocationsAdaptive")
    ):
        diag(tag, {"query": query, "variables": variables}, data, note="empty-result")
    return data.get("data", {})


ZONE_ANALYTICS_QUERY = """
query ZoneAnalytics($zoneTag: String!, $since: Date!, $until: Date!) {
  viewer {
    zones(filter: { zoneTag: $zoneTag }) {
      httpRequests1dGroups(
        limit: 366
        filter: { date_geq: $since, date_leq: $until }
        orderBy: [date_ASC]
      ) {
        dimensions { date }
        sum { requests bytes cachedRequests cachedBytes threats pageViews }
        uniq { uniques }
      }
    }
  }
}
"""

WORKER_ANALYTICS_QUERY = """
query WorkerAnalytics($accountTag: String!, $scriptName: String!, $since: DateTime!, $until: DateTime!) {
  viewer {
    accounts(filter: { accountTag: $accountTag }) {
      workersInvocationsAdaptive(
        limit: 10000
        filter: { scriptName: $scriptName datetime_geq: $since datetime_leq: $until }
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


@app.get("/")
async def dashboard():
    return FileResponse("cf-dash.html")


@app.get("/colors_and_type.css")
async def css():
    return FileResponse("colors_and_type.css", media_type="text/css")


@app.get("/config")
async def config():
    return {
        "has_server_token": CF_TOKEN is not None,
        "token_required": CF_TOKEN is None,
    }


@app.get("/zones")
async def get_zones(token: str = Query(None)):
    t0 = time.perf_counter()
    actual = resolve_token(token)
    headers = make_headers(actual)
    async with httpx.AsyncClient(timeout=30) as client:
        zones = await cf_paginate(client, f"{CF_API}/zones", headers, label="zones")
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
        log.info(
            f"[/zones] DONE  {len(result)} zones  {(time.perf_counter() - t0) * 1000:.0f}ms"
        )
        return {"total": len(result), "zones": result}


@app.get("/zones/{zone_id}/analytics")
async def get_zone_analytics(
    zone_id: str, token: str = Query(None), days: int = Query(7, ge=1, le=365)
):
    actual = resolve_token(token)
    headers = make_headers(actual)
    since, until = date_range(days)
    since_date, until_date = since[:10], until[:10]
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
        return {
            "zone_id": zone_id,
            "days": days,
            "period": {"since": since_date, "until": until_date},
            "daily": [],
            "totals": {},
        }
    groups = zones_data[0].get("httpRequests1dGroups", [])
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
        s, u = g.get("sum", {}), g.get("uniq", {})
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
    return {
        "zone_id": zone_id,
        "days": days,
        "period": {"since": since_date, "until": until_date},
        "daily": daily,
        "totals": totals,
    }


@app.get("/workers")
async def get_workers(token: str = Query(None)):
    t0 = time.perf_counter()
    actual = resolve_token(token)
    headers = make_headers(actual)
    async with httpx.AsyncClient(timeout=60) as client:
        accounts_data = await cf_get(
            client, f"{CF_API}/accounts", headers, label="accounts"
        )
        accounts = accounts_data.get("result", [])
        all_workers = []
        for account in accounts:
            acct_id, acct_name = account["id"], account["name"]
            try:
                scripts_data = await cf_get(
                    client,
                    f"{CF_API}/accounts/{acct_id}/workers/scripts",
                    headers,
                    label=f"scripts/{acct_name}",
                )
                scripts = scripts_data.get("result", [])
            except HTTPException as e:
                log.warning(f"[/workers] skipping {acct_name}: {e.detail}")
                continue
            for script in scripts:
                name = (
                    script.get("id")
                    or script.get("script_name")
                    or script.get("name", "")
                )
                routes, crons = [], []
                try:
                    rd = await cf_get(
                        client,
                        f"{CF_API}/accounts/{acct_id}/workers/scripts/{name}/routes",
                        headers,
                        label=f"routes/{name}",
                    )
                    routes = rd.get("result", [])
                except Exception:
                    pass
                try:
                    cd = await cf_get(
                        client,
                        f"{CF_API}/accounts/{acct_id}/workers/scripts/{name}/schedules",
                        headers,
                        label=f"schedules/{name}",
                    )
                    crons = cd.get("result", {}).get("schedules", [])
                except Exception:
                    pass
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
        log.info(
            f"[/workers] DONE  total={len(all_workers)}  active={active}  {(time.perf_counter() - t0) * 1000:.0f}ms"
        )
        return {
            "total": len(all_workers),
            "active": active,
            "inactive": len(all_workers) - active,
            "workers": all_workers,
        }


@app.get("/workers/{account_id}/{script_name}/analytics")
async def get_worker_analytics(
    account_id: str,
    script_name: str,
    token: str = Query(None),
    days: int = Query(7, ge=1, le=90),
):
    actual = resolve_token(token)
    headers = make_headers(actual)
    since, until = date_range(days)
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
        return {
            "account_id": account_id,
            "script_name": script_name,
            "days": days,
            "period": {"since": since, "until": until},
            "buckets": [],
            "totals": {},
        }
    invocations = accounts_data[0].get("workersInvocationsAdaptive", [])
    buckets = []
    totals = {"requests": 0, "errors": 0, "subrequests": 0}
    for inv in invocations:
        s, q, d = (
            inv.get("sum", {}),
            inv.get("quantiles", {}),
            inv.get("dimensions", {}),
        )
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
    return {
        "account_id": account_id,
        "script_name": script_name,
        "days": days,
        "period": {"since": since, "until": until},
        "buckets": buckets,
        "totals": totals,
    }


@app.post("/create-token")
async def create_token(
    email: str = Query(...),
    global_key: str = Query(...),
    name: str = Query("cf-inventory-token"),
):
    log.info(f"[/create-token] START  email={email}  name={name}")
    headers = {
        "X-Auth-Email": email,
        "X-Auth-Key": global_key,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{CF_API}/user", headers=headers)
        if r.status_code == 403:
            raise HTTPException(
                status_code=403, detail="Invalid email or Global API Key"
            )
        r.raise_for_status()
        ra = await client.get(
            f"{CF_API}/accounts", headers=headers, params={"per_page": 1}
        )
        ra.raise_for_status()
        accounts = ra.json().get("result", [])
        if not accounts:
            raise HTTPException(status_code=404, detail="No Cloudflare accounts found")
        account_id, account_name = accounts[0]["id"], accounts[0]["name"]
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

        payload = {
            "name": name,
            "policies": [
                {
                    "effect": "allow",
                    "resources": {"com.cloudflare.api.account.zone.*": "*"},
                    "permission_groups": [
                        get_group("Zone Read"),
                        get_group("Analytics Read"),
                    ],
                },
                {
                    "effect": "allow",
                    "resources": {f"com.cloudflare.api.account.{account_id}": "*"},
                    "permission_groups": [
                        get_group("Account Settings Read"),
                        get_group("Account Analytics Read"),
                        get_group("Analytics Read"),
                        get_group("Workers Scripts Read"),
                        get_group("Workers AI Read"),
                    ],
                },
            ],
        }
        rt = await client.post(f"{CF_API}/user/tokens", headers=headers, json=payload)
        data = rt.json()
        if not data.get("success"):
            raise HTTPException(
                status_code=502,
                detail=f"Token creation failed: {data.get('errors', [])}",
            )
        result = data.get("result", {})
        return {
            "success": True,
            "token": result.get("value"),
            "token_id": result.get("id"),
            "token_name": name,
            "account_id": account_id,
            "account_name": account_name,
        }


@app.get("/debug/permission-groups")
async def debug_permission_groups(
    email: str = Query(...), global_key: str = Query(...)
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
# AI
# ---------------------------------------------------------------------------
AI_PRICING = {
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
NEURONS_PRICE_USD = 0.011 / 1000


def estimate_cost(model_id: str, input_units: int, output_units: int = 0) -> dict:
    pricing = AI_PRICING.get(model_id)
    if not pricing:
        return {"neurons": None, "usd": None, "note": "pricing unknown for this model"}
    neurons_in = round(input_units * pricing["input"] / 1_000_000)
    neurons_out = round(output_units * (pricing.get("output") or 0) / 1_000_000)
    total_neurons = neurons_in + neurons_out
    usd = round(total_neurons * NEURONS_PRICE_USD, 6)
    return {
        "neurons_input": neurons_in,
        "neurons_output": neurons_out,
        "neurons_total": total_neurons,
        "usd": usd,
        "unit": pricing["unit"],
        "free_daily_limit": NEURONS_PER_DAY_FREE,
        "pct_of_free_daily": round(total_neurons / NEURONS_PER_DAY_FREE * 100, 2),
    }


@app.get("/ai/models")
async def get_ai_models(token: str = Query(None), account_id: str = Query(None)):
    actual = resolve_token(token)
    headers = make_headers(actual)
    async with httpx.AsyncClient(timeout=30) as client:
        if not account_id:
            ra = await cf_get(client, f"{CF_API}/accounts", headers, label="accounts")
            accounts = ra.get("result", [])
            if not accounts:
                raise HTTPException(status_code=404, detail="No accounts found")
            account_id = accounts[0]["id"]
        models = await cf_paginate(
            client,
            f"{CF_API}/accounts/{account_id}/ai/models/search",
            headers,
            label="ai/models/search",
        )
    if not models:
        result = [
            {
                "id": mid,
                "name": mid,
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
            for mid, p in AI_PRICING.items()
        ]
    else:
        result = []
        for m in models:
            mid = m.get("name", "")
            p = AI_PRICING.get(mid)
            result.append(
                {
                    "id": mid,
                    "name": mid,
                    "description": m.get("description"),
                    "task": m.get("task", {}).get("name") if m.get("task") else None,
                    "tags": m.get("tags", []),
                    "source": m.get("source", "hosted"),
                    "category": p["category"] if p else None,
                    "pricing": {
                        "unit": p["unit"],
                        "input_neurons_per_m": p["input"],
                        "output_neurons_per_m": p.get("output"),
                    }
                    if p
                    else None,
                }
            )
    categories = sorted({r["category"] for r in result if r["category"]})
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
    messages: str = Query(None),  # JSON история чата для LLM
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

        pricing = AI_PRICING.get(model, {})
        category = pricing.get("category", "LLM")

        if category == "ImageGen":
            payload = {"prompt": prompt}
        elif category == "Embeddings":
            payload = {"text": prompt}
        else:
            # LLM — используем переданную историю или строим из одного сообщения
            import json as _json

            if messages:
                try:
                    msgs = _json.loads(messages)
                except Exception:
                    raise HTTPException(status_code=400, detail="Invalid messages JSON")
            else:
                msgs = []
                if system:
                    msgs.append({"role": "system", "content": system})
                msgs.append({"role": "user", "content": prompt})
            payload = {"messages": msgs, "max_tokens": max_tokens}

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

        if r.status_code == 400:
            raise HTTPException(
                status_code=400, detail=f"Model rejected request: {r.text}"
            )
        if r.status_code == 403:
            raise HTTPException(
                status_code=403, detail="Token lacks Workers AI permission"
            )
        r.raise_for_status()

        if category == "ImageGen":
            import base64

            content_type = r.headers.get("content-type", "")
            if "image/" in content_type:
                image_b64 = base64.b64encode(r.content).decode()
                image_bytes = len(r.content)
            else:
                data = r.json()
                if not data.get("success"):
                    raise HTTPException(
                        status_code=502,
                        detail=f"AI run failed: {data.get('errors', [])}",
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

        else:
            # LLM / Embeddings
            data = r.json()
            if not data.get("success"):
                raise HTTPException(
                    status_code=502, detail=f"AI run failed: {data.get('errors', [])}"
                )
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
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False, log_level="warning")
