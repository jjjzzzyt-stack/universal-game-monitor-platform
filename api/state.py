"""Visitor-driven Vercel API for Universal Gaming Radar Platform.

Vercel functions do not keep a daemon or durable disk. The browser posts its
last verified cache, seen IDs, settings and history; this function refreshes
only providers that are due and returns a complete state plus updated
continuity for localStorage.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import radar  # noqa: E402

CLOUD_VERSION = radar.APP_VERSION + "-vercel"
MAX_BODY_BYTES = 2_500_000
MAX_EVENTS = 500
MAX_SEEN = 6000
ALLOWED_PROVIDERS = tuple(name for name in radar.PROVIDER_INTERVALS if name != "custom_health")
request_lock = threading.Lock()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_text(value: Any, limit: int = 500) -> str:
    return str(value or "")[:limit]


def bounded(value: Any, depth: int = 0) -> Any:
    """Bound browser continuity without destroying legitimate provider data."""
    if depth > 8:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, list):
        return [bounded(x, depth + 1) for x in value[:600]]
    if isinstance(value, dict):
        return {safe_text(k, 100): bounded(v, depth + 1) for k, v in list(value.items())[:200]}
    return safe_text(value, 500)


def sanitize_settings(raw: Any) -> dict[str, Any]:
    settings = radar.deep_merge(radar.DEFAULT_SETTINGS, raw if isinstance(raw, dict) else {})
    settings["region"] = settings.get("region") if settings.get("region") in {"IN", "US", "GB", "CA", "AU", "DE", "FR", "JP", "BR"} else "IN"
    settings["currency"] = {"IN": "INR", "US": "USD", "GB": "GBP", "CA": "CAD", "AU": "AUD", "DE": "EUR", "FR": "EUR", "JP": "JPY", "BR": "BRL"}[settings["region"]]
    settings["deal_min_discount"] = max(0, min(100, radar.safe_int(settings.get("deal_min_discount"), 75)))
    settings["watchlist"] = [safe_text(x, 80) for x in settings.get("watchlist", []) if safe_text(x, 80).strip()][:30]
    settings["steam_apps"] = [
        {"id": safe_text(x.get("id"), 12), "name": safe_text(x.get("name"), 100)}
        for x in settings.get("steam_apps", []) if isinstance(x, dict) and safe_text(x.get("id"), 12).isdigit()
    ][:15]
    settings["price_rules"] = [bounded(x) for x in settings.get("price_rules", []) if isinstance(x, dict)][:50]
    settings["favorites"] = [bounded(x) for x in settings.get("favorites", []) if isinstance(x, dict)][:200]
    # Arbitrary server-side URL fetching is deliberately unavailable on a public deployment.
    settings["custom_monitors"] = []
    return settings


def new_continuity(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    cache_raw = raw.get("cache") if isinstance(raw.get("cache"), dict) else {}
    cache = {k: bounded(v) for k, v in cache_raw.items() if k in set(ALLOWED_PROVIDERS) | {"steam", "live"}}
    meta_raw = raw.get("provider_meta") if isinstance(raw.get("provider_meta"), dict) else {}
    provider_meta = {}
    for name in ALLOWED_PROVIDERS:
        m = meta_raw.get(name) if isinstance(meta_raw.get(name), dict) else {}
        provider_meta[name] = {
            "ok": m.get("ok") if isinstance(m.get("ok"), bool) else None,
            "last_success": max(0.0, safe_float(m.get("last_success"))),
            "last_attempt": max(0.0, safe_float(m.get("last_attempt"))),
            "last_error": safe_text(m.get("last_error"), 600) or None,
            "latency": max(0.0, safe_float(m.get("latency"))) if m.get("latency") is not None else None,
            "items": max(0, min(10000, radar.safe_int(m.get("items")))),
        }
    seen_raw = raw.get("seen") if isinstance(raw.get("seen"), dict) else {}
    seen = {safe_text(k, 100): [safe_text(x, 240) for x in v[-MAX_SEEN:]] for k, v in seen_raw.items() if isinstance(v, list)}
    return {
        "settings": sanitize_settings(raw.get("settings")),
        "cache": cache,
        "provider_meta": provider_meta,
        "seen": seen,
        "baselined": {safe_text(k, 100): bool(v) for k, v in (raw.get("baselined") or {}).items()} if isinstance(raw.get("baselined"), dict) else {},
        "service_states": {safe_text(k, 160): safe_text(v, 30) for k, v in (raw.get("service_states") or {}).items()} if isinstance(raw.get("service_states"), dict) else {},
        "live_index": max(0, radar.safe_int(raw.get("live_index"))),
        "events": [bounded(x) for x in raw.get("events", []) if isinstance(x, dict)][-MAX_EVENTS:],
        "metrics": [bounded(x) for x in raw.get("metrics", []) if isinstance(x, dict)][-720:],
        "settings_fingerprint": safe_text(raw.get("settings_fingerprint"), 80),
    }


def fingerprint(settings: dict[str, Any]) -> str:
    meaningful = {"region": settings.get("region"), "watchlist": settings.get("watchlist"), "steam_apps": settings.get("steam_apps")}
    return hashlib.sha256(json.dumps(meaningful, sort_keys=True).encode()).hexdigest()[:24]


def append_event(c: dict[str, Any], new_events: list[dict[str, Any]], kind: str, title: str, message: str, level: str = "info", url: str = "", category: str = "", notify: bool = False) -> None:
    event = {"id": f"{time.time_ns():x}", "time": now_iso(), "kind": kind, "title": safe_text(title, 240), "message": safe_text(message, 1000), "level": level, "url": safe_text(url, 1600), "category": category, "notify": bool(notify)}
    c["events"].append(event)
    c["events"] = c["events"][-MAX_EVENTS:]
    new_events.append(event)


def execute_provider(name: str, c: dict[str, Any]) -> dict[str, Any]:
    settings = c["settings"]
    if name == "live_radar":
        watchlist = settings.get("watchlist", [])
        if not watchlist:
            return {"kind": "live", "game": "", "items": []}
        index = c["live_index"] % len(watchlist)
        game = watchlist[index]
        c["live_index"] = (index + 1) % len(watchlist)
        return radar.provider_live_game(game)
    if name == "epic_free":
        return radar.provider_epic_free(settings.get("region", "IN"))
    if name == "steam_featured":
        return radar.provider_steam_featured(settings.get("region", "IN"))
    if name == "gog_deals":
        return radar.provider_gog_deals(settings.get("region", "IN"))
    if name == "steam_news":
        return radar.provider_steam_news(settings.get("steam_apps", []))
    return radar.PROVIDER_FUNCTIONS[name]()


def price_matches(settings: dict[str, Any], item: dict[str, Any]) -> bool:
    title = str(item.get("title", "")).lower()
    discount = radar.safe_int(item.get("discount"))
    price = radar.price_number(item.get("sale_price"))
    for rule in settings.get("price_rules", []):
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        keyword = str(rule.get("keyword", "")).strip().lower()
        if keyword and keyword not in title:
            continue
        if discount < radar.safe_int(rule.get("min_discount")):
            continue
        max_price = rule.get("max_price")
        if max_price not in {None, ""} and (price is None or price > safe_float(max_price, -1)):
            continue
        return True
    return False


def process_services(c: dict[str, Any], items: list[dict[str, Any]], new_events: list[dict[str, Any]]) -> None:
    states = c["service_states"]
    for item in items:
        service_id = safe_text(item.get("id"), 160)
        current = safe_text(item.get("status"), 30)
        previous = states.get(service_id)
        states[service_id] = current
        if previous is None and current not in {"operational", "unknown"}:
            append_event(c, new_events, "service_incident", f"{item.get('name')} issue detected", item.get("description", current), "warning", item.get("url", ""), "service_incidents", True)
        elif previous and previous != current:
            if current == "operational":
                append_event(c, new_events, "service_recovery", f"{item.get('name')} recovered", "Official status is operational again.", "success", item.get("url", ""), "service_recoveries", True)
            elif current != "unknown":
                append_event(c, new_events, "service_incident", f"{item.get('name')} status changed", f"New status: {current}.", "warning", item.get("url", ""), "service_incidents", True)
    c["baselined"]["services"] = True


def process_items(c: dict[str, Any], provider: str, kind: str, items: list[dict[str, Any]], new_events: list[dict[str, Any]]) -> None:
    key = "live" if kind == "live" else provider
    seen_list = list(c["seen"].get(key, []))
    seen = set(seen_list)
    baseline = not c["baselined"].get(key)
    discovered = []
    for item in items:
        item_id = safe_text(item.get("id"), 240)
        if item_id and item_id not in seen:
            seen.add(item_id)
            seen_list.append(item_id)
            if not baseline:
                discovered.append(item)
    c["seen"][key] = seen_list[-MAX_SEEN:]
    c["baselined"][key] = True
    if baseline:
        append_event(c, new_events, "baseline", f"{provider.replace('_', ' ').title()} baseline ready", f"Recorded {len(items)} current items silently.", "success")
        return
    for item in discovered[:10]:
        if kind == "free_games" and item.get("status") == "free_now":
            append_event(c, new_events, "free_game", "New free game", f"{item.get('title')} is free on {item.get('store')}.", "success", item.get("url", ""), "free_games", True)
        elif kind == "giveaways":
            append_event(c, new_events, "giveaway", "New gaming giveaway", item.get("title", "Giveaway"), "success", item.get("url", ""), "giveaways", True)
        elif kind == "deals":
            if price_matches(c["settings"], item):
                append_event(c, new_events, "price_watch", "Price watch matched", f"{item.get('title')} · {item.get('sale_price', '')} · {item.get('discount', 0)}% off", "success", item.get("url", ""), "price_watches", True)
            elif radar.safe_int(item.get("discount")) >= radar.safe_int(c["settings"].get("deal_min_discount"), 75):
                append_event(c, new_events, "deal", f"{item.get('discount')}% gaming deal", item.get("title", "Deal"), "info", item.get("url", ""), "deals", True)
        elif kind == "live":
            append_event(c, new_events, "live", f"{item.get('game')} stream discovered", item.get("title", "Live gameplay"), "info", item.get("url", ""), "live_streams", True)
        elif kind == "news":
            append_event(c, new_events, "news", f"{item.get('game')} news", item.get("title", "Game update"), "info", item.get("url", ""), "game_news", True)


def store_result(c: dict[str, Any], provider: str, result: dict[str, Any], new_events: list[dict[str, Any]]) -> None:
    kind = result.get("kind")
    items = result.get("items", []) if isinstance(result.get("items", []), list) else []
    if kind == "live":
        game = safe_text(result.get("game"), 80)
        live = c["cache"].setdefault("live", {})
        if game:
            live[game] = bounded(items)
    elif kind == "steam":
        c["cache"]["steam"] = bounded(result.get("sections", {}))
    else:
        c["cache"][provider] = bounded(items)
    if kind == "services":
        process_services(c, items, new_events)
    elif kind in {"free_games", "giveaways", "deals", "live", "news"}:
        process_items(c, provider, kind, items, new_events)
    elif kind == "steam":
        process_items(c, provider, "deals", items, new_events)


def flatten_state(c: dict[str, Any], new_events: list[dict[str, Any]]) -> dict[str, Any]:
    cache = c["cache"]
    services = []
    for name in ("epic_status", "discord_status", "playstation_status", "xbox_status", "steam_health", "twitch_status", "roblox_status"):
        services.extend(cache.get(name, []) if isinstance(cache.get(name), list) else [])
    free_games = cache.get("epic_free", []) if isinstance(cache.get("epic_free"), list) else []
    giveaways = cache.get("gamerpower", []) if isinstance(cache.get("gamerpower"), list) else []
    cheap = cache.get("cheapshark", []) if isinstance(cache.get("cheapshark"), list) else []
    gog = cache.get("gog_deals", []) if isinstance(cache.get("gog_deals"), list) else []
    deals = cheap + gog
    steam = cache.get("steam", {}) if isinstance(cache.get("steam"), dict) else {}
    news = cache.get("steam_news", []) if isinstance(cache.get("steam_news"), list) else []
    catalog = cache.get("free_to_play", []) if isinstance(cache.get("free_to_play"), list) else []
    live_map = cache.get("live", {}) if isinstance(cache.get("live"), dict) else {}
    live = []
    for game in c["settings"].get("watchlist", []):
        if isinstance(live_map.get(game), list):
            live.extend(live_map[game])
    healthy = sum(1 for x in services if x.get("status") == "operational")
    incidents = sum(1 for x in services if x.get("status") not in {"operational", "unknown"})
    meta_view = {}
    now = time.time()
    for name in ALLOWED_PROVIDERS:
        m = c["provider_meta"][name]
        elapsed = max(0.0, now - m.get("last_success", 0))
        meta_view[name] = {
            "ok": m.get("ok"), "last_success": dt.datetime.fromtimestamp(m["last_success"], dt.timezone.utc).isoformat().replace("+00:00", "Z") if m.get("last_success") else None,
            "last_error": m.get("last_error"), "latency": m.get("latency"), "items": m.get("items", 0),
            "next_in": max(0, round(radar.PROVIDER_INTERVALS[name] - elapsed)), "interval": radar.PROVIDER_INTERVALS[name],
        }
    last_read = radar.parse_iso(c["settings"].get("last_read_at"))
    unread = sum(1 for event in c["events"] if not last_read or (radar.parse_iso(event.get("time")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) > last_read)
    return {
        "app": {"name": radar.APP_NAME, "version": CLOUD_VERSION, "port": None, "started_at": None},
        "runtime": {"started_at": None, "checking": False, "last_cycle": now_iso(), "last_error": "; ".join(f"{k}: {v.get('last_error')}" for k, v in meta_view.items() if v.get("ok") is False) or None},
        "providers": meta_view, "settings": c["settings"], "services": services,
        "free_games": free_games, "giveaways": giveaways, "deals": deals, "cheapshark_deals": cheap, "gog_deals": gog,
        "steam": steam, "news": news, "free_to_play": catalog,
        "favorites": c["settings"].get("favorites", []), "metrics": c["metrics"],
        "live_streams": live, "live_by_game": live_map,
        "events": list(reversed(c["events"][-300:])), "unread_events": unread,
        "stats": {
            "services_healthy": healthy, "service_incidents": incidents, "services_total": len(services),
            "free_now": sum(1 for x in free_games if x.get("status") == "free_now"), "giveaways": len(giveaways),
            "deals": len(deals), "live": len(live), "news": len(news), "free_to_play": len(catalog),
            "favorites": len(c["settings"].get("favorites", [])), "price_rules": len(c["settings"].get("price_rules", [])),
            "providers_healthy": sum(1 for x in meta_view.values() if x.get("ok") is True), "providers_total": len(meta_view),
        },
        "notification_backend": "Browser Notifications · active tab/PWA only",
        "generated_at": now_iso(), "serverlessMode": True, "new_events": new_events,
        "limitations": {"background": "Vercel has no permanent monitor loop; refreshes are visitor-driven.", "persistence": "Settings, cache and history are held in this browser's localStorage.", "notifications": "Browser notifications require the dashboard/PWA to be open."},
        "continuity": c,
    }


def build_state(body: Any) -> dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    c = new_continuity(body.get("continuity") if isinstance(body.get("continuity"), dict) else body)
    new_events: list[dict[str, Any]] = []
    current_fingerprint = fingerprint(c["settings"])
    if c.get("settings_fingerprint") != current_fingerprint:
        for name in ("epic_free", "steam_featured", "gog_deals", "steam_news", "live_radar"):
            c["provider_meta"][name]["last_success"] = 0
        c["settings_fingerprint"] = current_fingerprint
    force = bool(body.get("force"))
    now = time.time()
    due = []
    for name in ALLOWED_PROVIDERS:
        meta = c["provider_meta"][name]
        cache_key = "steam" if name == "steam_featured" else "live" if name == "live_radar" else name
        missing = cache_key not in c["cache"]
        last = meta.get("last_success", 0)
        failed_retry = meta.get("ok") is False and now - meta.get("last_attempt", 0) >= min(30, radar.PROVIDER_INTERVALS[name])
        if force or missing or failed_retry or now - last >= radar.PROVIDER_INTERVALS[name]:
            due.append(name)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(due)))) as pool:
        futures = {pool.submit(execute_provider, name, c): (name, time.monotonic()) for name in due}
        for future in concurrent.futures.as_completed(futures):
            name, started = futures[future]
            meta = c["provider_meta"][name]
            meta["last_attempt"] = now
            meta["latency"] = round(time.monotonic() - started, 2)
            was_error = meta.get("ok") is False
            try:
                result = future.result()
                store_result(c, name, result, new_events)
                meta.update(ok=True, last_success=time.time(), last_error=None, items=len(result.get("items", [])) if isinstance(result.get("items", []), list) else 0)
                if was_error:
                    append_event(c, new_events, "provider_recovery", f"{name.replace('_', ' ').title()} recovered", "Provider is responding again.", "success")
            except Exception as exc:
                first = meta.get("ok") is not False
                meta.update(ok=False, last_error=f"{type(exc).__name__}: {exc}")
                if first:
                    append_event(c, new_events, "provider_error", f"{name.replace('_', ' ').title()} failed", meta["last_error"], "error")
    metrics = c["metrics"]
    last_metric = radar.parse_iso(metrics[-1].get("time")) if metrics else None
    if not last_metric or (dt.datetime.now(dt.timezone.utc) - last_metric).total_seconds() >= 55:
        latencies = [x.get("latency") for x in c["provider_meta"].values() if isinstance(x.get("latency"), (int, float))]
        metrics.append({"time": now_iso(), "healthy": sum(1 for x in c["provider_meta"].values() if x.get("ok") is True), "total": len(ALLOWED_PROVIDERS), "incidents": sum(1 for x in c["service_states"].values() if x not in {"operational", "unknown"}), "avg_latency": round(sum(latencies) / len(latencies), 2) if latencies else None})
        c["metrics"] = metrics[-720:]
    return flatten_state(c, new_events)


class handler(BaseHTTPRequestHandler):
    def send_json(self, payload: Any, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Gaming-Radar-Version", CLOUD_VERSION)
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_body(self) -> dict[str, Any]:
        try:
            length = min(max(0, int(self.headers.get("Content-Length", "0"))), MAX_BODY_BYTES)
        except ValueError:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def respond(self, body: dict[str, Any]) -> None:
        try:
            with request_lock:
                self.send_json(build_state(body))
        except Exception as exc:
            self.send_json({"error": f"{type(exc).__name__}: {exc}", "version": CLOUD_VERSION}, 500)

    def do_GET(self) -> None:
        self.respond({})

    def do_POST(self) -> None:
        self.respond(self.read_body())
