#!/usr/bin/env python3
"""
Scrape ~1,000+ Manhattan restaurants from the Yelp API into DynamoDB.

Usage:
    export YELP_API_KEY="your-yelp-key"
    export AWS_REGION="us-east-1"        # same region as the rest of your project
    python scrape_yelp.py

Requires:  pip install boto3 requests
AWS credentials come from `aws configure` (or environment variables).
Never hard-code or commit your API key.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import requests

# ---------------------------------------------------------------- config
API_KEY = os.environ.get("YELP_API_KEY")
REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = "yelp-restaurants"
SEARCH_URL = "https://api.yelp.com/v3/businesses/search"

# Keep these identical to the values of your Lex "CuisineType" slot type.
CUISINES = ["Chinese", "Italian", "Japanese", "Mexican", "Indian", "Thai"]

TARGET_PER_CUISINE = 200   # assignment: "200 or so" per cuisine
PAGE_SIZE = 50             # Yelp maximum per request
MAX_OFFSET = 1000          # safety ceiling; Yelp may stop earlier (we handle that)
SORT_MODES = ["best_match", "rating", "review_count"]  # different orderings -> more unique results
LOCATION = "Manhattan, NY"
SLEEP_BETWEEN_CALLS = 0.4  # seconds, to stay polite with rate limits
BACKUP_FILE = "restaurants_backup.json"


# ---------------------------------------------------------------- Yelp
def fetch_page(term, sort_by, offset):
    """Return a list of businesses for one page, or [] if there are no more."""
    headers = {"Authorization": f"Bearer {API_KEY}"}
    params = {
        "term": term,
        "location": LOCATION,
        "limit": PAGE_SIZE,
        "offset": offset,
        "sort_by": sort_by,
    }
    for attempt in range(5):
        resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
        if resp.status_code == 429:  # rate limited -> back off and retry
            wait = 2 ** attempt
            print(f"    rate limited, waiting {wait}s ...")
            time.sleep(wait)
            continue
        if resp.status_code == 400:  # usually "offset too large" -> no more pages
            return []
        if resp.status_code in (401, 403):
            sys.exit(f"Yelp rejected the API key (HTTP {resp.status_code}): {resp.text}")
        resp.raise_for_status()
        return resp.json().get("businesses", [])
    raise RuntimeError("Too many rate-limit retries; try again later.")


# ---------------------------------------------------------------- DynamoDB item
def to_item(biz, cuisine):
    """Convert a Yelp business into a DynamoDB-safe item (no floats, no empty strings)."""
    loc = biz.get("location") or {}
    address = ", ".join(loc.get("display_address") or []) or (loc.get("address1") or "N/A")

    item = {
        "BusinessID": biz["id"],
        "Name": biz.get("name") or "N/A",
        "Address": address,
        "NumReviews": int(biz.get("review_count") or 0),
        "Rating": Decimal(str(biz.get("rating") or 0)),
        "ZipCode": loc.get("zip_code") or "N/A",
        "Cuisine": cuisine,
        "insertedAtTimestamp": datetime.now(timezone.utc).isoformat(),
    }

    coords = biz.get("coordinates") or {}
    lat, lon = coords.get("latitude"), coords.get("longitude")
    if lat is not None and lon is not None:
        item["Coordinates"] = {
            "Latitude": Decimal(str(lat)),
            "Longitude": Decimal(str(lon)),
        }
    return item


# ---------------------------------------------------------------- main
def main():
    if not API_KEY:
        sys.exit("Set the YELP_API_KEY environment variable first.")

    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)

    seen_ids = set()   # dedupe across ALL cuisines
    all_items = []

    for cuisine in CUISINES:
        print(f"\n=== {cuisine} ===")
        term = f"{cuisine} restaurants"
        cuisine_items = []

        for sort_by in SORT_MODES:
            if len(cuisine_items) >= TARGET_PER_CUISINE:
                break
            offset = 0
            while offset < MAX_OFFSET and len(cuisine_items) < TARGET_PER_CUISINE:
                businesses = fetch_page(term, sort_by, offset)
                time.sleep(SLEEP_BETWEEN_CALLS)
                if not businesses:
                    break
                new = 0
                for biz in businesses:
                    if biz["id"] in seen_ids:
                        continue
                    seen_ids.add(biz["id"])
                    cuisine_items.append(to_item(biz, cuisine))
                    new += 1
                    if len(cuisine_items) >= TARGET_PER_CUISINE:
                        break
                print(f"  sort={sort_by:<12} offset={offset:<4} +{new} new "
                      f"(total {len(cuisine_items)})")
                offset += PAGE_SIZE

        # write this cuisine to DynamoDB right away, so progress isn't lost
        with table.batch_writer(overwrite_by_pkeys=["BusinessID"]) as batch:
            for item in cuisine_items:
                batch.put_item(Item=item)
        print(f"  -> wrote {len(cuisine_items)} {cuisine} restaurants")
        all_items.extend(cuisine_items)

    # local backup (handy for loading OpenSearch later without re-calling Yelp)
    with open(BACKUP_FILE, "w") as f:
        json.dump(all_items, f, default=str, indent=2)

    print(f"\nDone. {len(all_items)} unique restaurants saved to '{TABLE_NAME}'.")
    print(f"Backup written to {BACKUP_FILE}")
    if len(all_items) < 1000:
        print("WARNING: fewer than 1,000 restaurants. Add another cuisine to CUISINES "
              "and run again (already-saved restaurants are simply overwritten).")


if __name__ == "__main__":
    main()