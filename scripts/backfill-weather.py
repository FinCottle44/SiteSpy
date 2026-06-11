#!/usr/bin/env python3
"""SiteSpy — Backfill Weather Data for Existing Snapshots.

Scans IMG# records that are missing a `weather` attribute and fetches
historical weather from OpenWeather's Timemachine API (One Call 3.0)
for the snapshot's actual timestamp.

Requires:
  - OpenWeather One Call 3.0 API key (supports historical lookups)
    Set via OPENWEATHER_API_KEY env var or --api-key flag
  - AWS credentials with DynamoDB read/write access (uses profile from --profile)
  - boto3, installed in your local Python environment

Usage:
  export OPENWEATHER_API_KEY=your_key_here

  # Backfill all snapshots for a site (dry-run first)
  python scripts/backfill-weather.py --tenant red_construction --site red_wsm --dry-run

  # Actually backfill
  python scripts/backfill-weather.py --tenant red_construction --site red_wsm

  # Backfill a specific camera only
  python scripts/backfill-weather.py --tenant red_construction --site red_wsm --camera cam_01

  # Limit to N records
  python scripts/backfill-weather.py --tenant red_construction --site red_wsm --limit 50

  # Use current weather instead of historical (free tier, less accurate)
  python scripts/backfill-weather.py --tenant red_construction --site red_wsm --use-current

Notes:
  - This script targets the DEV environment only.
  - The script only updates records where `weather` attribute is missing.
  - OpenWeather Timemachine API: 1000 calls/day on One Call 3.0.
  - Rate-limited to ~1 request/second to avoid hitting OpenWeather limits.
  - Safe to re-run (idempotent — skips records that already have weather).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

import boto3

# ─── Constants ───────────────────────────────────────────────────────────────

OPENWEATHER_CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
OPENWEATHER_TIMEMACHINE_URL = "https://api.openweathermap.org/data/3.0/onecall/timemachine"
RATE_LIMIT_SECONDS = 1.1  # Stay under 60 calls/min
DEFAULT_REGION = "eu-west-2"
DEFAULT_PROFILE = "sitespy-dev"

# ─── Colors ──────────────────────────────────────────────────────────────────

GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
DIM = "\033[0;90m"
NC = "\033[0m"


# ─── Weather Fetchers ────────────────────────────────────────────────────────


def fetch_historical_weather(
    lat: float, lon: float, timestamp_unix: int, api_key: str
) -> dict | None:
    """Fetch weather for a specific past timestamp using One Call Timemachine."""
    url = (
        f"{OPENWEATHER_TIMEMACHINE_URL}"
        f"?lat={lat}&lon={lon}&dt={timestamp_unix}&appid={api_key}&units=metric"
    )
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # Timemachine returns { "data": [ { ... } ] }
        records = data.get("data", [])
        if not records:
            return None

        record = records[0]
        weather_list = record.get("weather", [{}])
        weather_block = weather_list[0] if weather_list else {}

        return {
            "condition": weather_block.get("main", "Unknown"),
            "description": weather_block.get("description", ""),
            "temp_c": round(float(record.get("temp", 0)), 1),
            "feels_like_c": round(float(record.get("feels_like", 0)), 1),
            "humidity_pct": int(record.get("humidity", 0)),
            "wind_speed_ms": round(float(record.get("wind_speed", 0)), 1),
            "wind_deg": int(record.get("wind_deg", 0)),
            "visibility_m": int(record.get("visibility", 10000)),
            "cloud_pct": int(record.get("clouds", 0)),
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  {RED}Network error: {exc}{NC}")
        return None
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        print(f"  {RED}Parse error: {exc}{NC}")
        return None


def fetch_current_weather(lat: float, lon: float, api_key: str) -> dict | None:
    """Fetch current weather (free tier fallback)."""
    url = (
        f"{OPENWEATHER_CURRENT_URL}"
        f"?lat={lat}&lon={lon}&appid={api_key}&units=metric"
    )
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        weather_block = data.get("weather", [{}])[0]
        main_block = data.get("main", {})
        wind_block = data.get("wind", {})

        return {
            "condition": weather_block.get("main", "Unknown"),
            "description": weather_block.get("description", ""),
            "temp_c": round(float(main_block.get("temp", 0)), 1),
            "feels_like_c": round(float(main_block.get("feels_like", 0)), 1),
            "humidity_pct": int(main_block.get("humidity", 0)),
            "wind_speed_ms": round(float(wind_block.get("speed", 0)), 1),
            "wind_deg": int(wind_block.get("deg", 0)),
            "visibility_m": int(data.get("visibility", 10000)),
            "cloud_pct": int(data.get("clouds", {}).get("all", 0)),
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  {RED}Network error: {exc}{NC}")
        return None
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        print(f"  {RED}Parse error: {exc}{NC}")
        return None


# ─── DynamoDB Helpers ────────────────────────────────────────────────────────


def weather_to_dynamo_map(weather: dict) -> dict:
    """Convert weather dict to DynamoDB M attribute."""
    return {
        "M": {
            "condition": {"S": weather["condition"]},
            "description": {"S": weather["description"]},
            "temp_c": {"N": str(weather["temp_c"])},
            "feels_like_c": {"N": str(weather["feels_like_c"])},
            "humidity_pct": {"N": str(weather["humidity_pct"])},
            "wind_speed_ms": {"N": str(weather["wind_speed_ms"])},
            "wind_deg": {"N": str(weather["wind_deg"])},
            "visibility_m": {"N": str(weather["visibility_m"])},
            "cloud_pct": {"N": str(weather["cloud_pct"])},
        }
    }


def iso_to_unix(iso_ts: str) -> int:
    """Convert ISO8601 timestamp (e.g. 2026-06-09T14:28:44Z) to Unix timestamp."""
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return int(dt.timestamp())


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Backfill weather data for existing SiteSpy snapshots."
    )
    parser.add_argument("--tenant", required=True, help="Tenant ID")
    parser.add_argument("--site", required=True, help="Site ID")
    parser.add_argument("--camera", default=None, help="Camera ID (optional, all cameras if omitted)")
    parser.add_argument("--limit", type=int, default=None, help="Max records to process")
    parser.add_argument("--table", default=None, help="DynamoDB table name (auto-detected from env)")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="AWS CLI profile")
    parser.add_argument("--region", default=DEFAULT_REGION, help="AWS region")
    parser.add_argument("--api-key", default=None, help="OpenWeather API key (or set OPENWEATHER_API_KEY)")
    parser.add_argument("--use-current", action="store_true", help="Use current weather (free tier) instead of historical")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without writing")

    args = parser.parse_args()

    # Resolve API key
    api_key = args.api_key or os.environ.get("OPENWEATHER_API_KEY", "")
    if not api_key:
        print(f"{RED}Error: No OpenWeather API key provided.{NC}")
        print("Set OPENWEATHER_API_KEY env var or pass --api-key")
        sys.exit(1)

    # Resolve table name (dev only)
    table_name = args.table or "sitespy-dev-data"

    # Set up boto3 session
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ddb = session.client("dynamodb")

    print(f"{YELLOW}═══════════════════════════════════════════════════════════{NC}")
    print(f"{YELLOW}  SiteSpy — Backfill Weather{NC}")
    print(f"{YELLOW}═══════════════════════════════════════════════════════════{NC}")
    print()
    print(f"  Table:    {table_name}")
    print(f"  Tenant:   {args.tenant}")
    print(f"  Site:     {args.site}")
    print(f"  Camera:   {args.camera or 'all'}")
    print(f"  Mode:     {'current weather (free)' if args.use_current else 'historical (One Call 3.0)'}")
    if args.limit:
        print(f"  Limit:    {args.limit}")
    if args.dry_run:
        print(f"  {YELLOW}DRY RUN — no writes{NC}")
    print()

    # ─── Fetch site lat/lng ──────────────────────────────────────────────────
    print("Fetching site coordinates... ", end="", flush=True)
    site_resp = ddb.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": f"TENANT#{args.tenant}"},
            "SK": {"S": f"SITE#{args.site}"},
        },
    )
    site_item = site_resp.get("Item")
    if not site_item:
        print(f"{RED}Site not found!{NC}")
        sys.exit(1)

    lat_val = site_item.get("latitude", {}).get("N")
    lon_val = site_item.get("longitude", {}).get("N")
    if not lat_val or not lon_val:
        print(f"{RED}Site has no lat/lng configured!{NC}")
        print("Use PATCH /v1/sites/{site_id} to set latitude and longitude first.")
        sys.exit(1)

    lat = float(lat_val)
    lon = float(lon_val)
    print(f"{GREEN}{lat}, {lon}{NC}")

    # ─── Query IMG# records missing weather ──────────────────────────────────
    print("Scanning for snapshots without weather... ", end="", flush=True)

    sk_prefix = f"IMG#{args.site}#"
    if args.camera:
        sk_prefix = f"IMG#{args.site}#{args.camera}#"

    kwargs = {
        "TableName": table_name,
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
        "FilterExpression": "attribute_not_exists(weather)",
        "ExpressionAttributeValues": {
            ":pk": {"S": f"TENANT#{args.tenant}"},
            ":sk": {"S": sk_prefix},
        },
    }

    items_to_update = []
    while True:
        response = ddb.query(**kwargs)
        items_to_update.extend(response.get("Items", []))
        if args.limit and len(items_to_update) >= args.limit:
            items_to_update = items_to_update[: args.limit]
            break
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key

    print(f"{GREEN}{len(items_to_update)} records{NC}")

    if not items_to_update:
        print(f"\n{GREEN}Nothing to backfill — all snapshots already have weather data.{NC}")
        return

    # ─── Process each record ─────────────────────────────────────────────────
    print()
    updated = 0
    failed = 0
    skipped = 0

    for i, item in enumerate(items_to_update, 1):
        sk = item["SK"]["S"]
        ingested_at = item.get("ingested_at", {}).get("S", "")

        if not ingested_at:
            print(f"  [{i}/{len(items_to_update)}] {DIM}No timestamp, skipping{NC}")
            skipped += 1
            continue

        print(f"  [{i}/{len(items_to_update)}] {ingested_at} — ", end="", flush=True)

        # Fetch weather
        if args.use_current:
            weather = fetch_current_weather(lat, lon, api_key)
        else:
            unix_ts = iso_to_unix(ingested_at)
            weather = fetch_historical_weather(lat, lon, unix_ts, api_key)

        if weather is None:
            print(f"{RED}failed to fetch weather{NC}")
            failed += 1
            # Rate limit even on failures
            time.sleep(RATE_LIMIT_SECONDS)
            continue

        if args.dry_run:
            print(f"{YELLOW}would set: {weather['condition']}, {weather['temp_c']}°C{NC}")
            updated += 1
        else:
            # Write weather to DynamoDB
            try:
                ddb.update_item(
                    TableName=table_name,
                    Key={
                        "PK": item["PK"],
                        "SK": item["SK"],
                    },
                    UpdateExpression="SET weather = :w",
                    ExpressionAttributeValues={
                        ":w": weather_to_dynamo_map(weather),
                    },
                )
                print(f"{GREEN}{weather['condition']}, {weather['temp_c']}°C{NC}")
                updated += 1
            except Exception as exc:
                print(f"{RED}DynamoDB write failed: {exc}{NC}")
                failed += 1

        # Rate limit
        time.sleep(RATE_LIMIT_SECONDS)

    # ─── Summary ─────────────────────────────────────────────────────────────
    print()
    print(f"{GREEN}═══════════════════════════════════════════════════════════{NC}")
    print(f"{GREEN}  Done!{NC}")
    print(f"{GREEN}═══════════════════════════════════════════════════════════{NC}")
    print()
    print(f"  Updated: {updated}")
    print(f"  Failed:  {failed}")
    print(f"  Skipped: {skipped}")
    print()
    if args.dry_run:
        print(f"  {YELLOW}This was a dry run — nothing was written.{NC}")
        print("  Remove --dry-run to execute for real.")


if __name__ == "__main__":
    main()
