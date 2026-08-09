#!/usr/bin/env python3
"""
Fast Lane Spares - B2B Pricing DRY RUN

READ ONLY:
- Reads Shopify products/variants.
- Calculates proposed B2B pricing from the existing B2B Excel rules.
- Does NOT write or change anything in Shopify.
- Produces a CSV audit in /output.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import requests


# ---------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------

API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-07")

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

D = Decimal

NORMAL_FREIGHT_PER_KG = D("8")
HOLLEY_DIRECT_FREIGHT_PER_KG = D("10")

# Existing Excel margin floors:
# Landed cost <= $1,500 -> 25% minimum margin
# Landed cost >  $1,500 -> 20% minimum margin
LOW_COST_MARGIN = D("0.25")
HIGH_COST_MARGIN = D("0.20")
MARGIN_BREAKPOINT = D("1500")


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def dec(value, default=None):
    if value is None:
        return default

    s = str(value).strip()

    if not s:
        return default

    try:
        return D(s.replace(",", ""))
    except InvalidOperation:
        return default


def round_1(value: D) -> D:
    return value.quantize(D("0.1"), rounding=ROUND_HALF_UP)


def money(value) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"


def pct(value) -> str:
    if value is None:
        return ""
    return f"{value * D('100'):.2f}%"


def normalize(value: str) -> str:
    return (value or "").strip().lower()


# ---------------------------------------------------------
# SHOPIFY
# ---------------------------------------------------------

def get_shopify_token(store: str, client_id: str, client_secret: str) -> str:
    url = f"https://{store}/admin/oauth/access_token"

    response = requests.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
        timeout=60,
    )

    response.raise_for_status()

    payload = response.json()

    token = payload.get("access_token")

    if not token:
        raise RuntimeError(
            f"Shopify token response did not include access_token: {payload}"
        )

    print("Shopify authentication OK.")

    return token


def graphql(store: str, token: str, query: str, variables: dict) -> dict:
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"

    while True:
        response = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": token,
            },
            json={
                "query": query,
                "variables": variables,
            },
            timeout=90,
        )

        if response.status_code in (429, 500, 502, 503, 504):
            wait = int(response.headers.get("Retry-After", "3"))
            print(
                f"Shopify HTTP {response.status_code}; "
                f"retrying in {wait}s..."
            )
            time.sleep(wait)
            continue

        response.raise_for_status()

        payload = response.json()

        errors = payload.get("errors") or []

        if errors:
            throttled = any(
                (error.get("extensions") or {}).get("code") == "THROTTLED"
                for error in errors
            )

            if throttled:
                print("Shopify throttled; retrying in 5s...")
                time.sleep(5)
                continue

            raise RuntimeError(json.dumps(errors, indent=2))

        return payload


def fetch_shopify_variants(store: str, token: str) -> list[dict]:

    # This query belongs ONLY to the B2B script.
    # Existing Holley updater does not need to be modified.

    query = """
    query FastLaneB2BVariants(
        $first: Int!,
        $after: String
    ) {
      productVariants(
        first: $first,
        after: $after,
        sortKey: ID
      ) {
        nodes {
          id
          sku
          price

          product {
            id
            title
            handle
            vendor
            tags
          }

          inventoryItem {
            id

            unitCost {
              amount
              currencyCode
            }

            measurement {
              weight {
                value
                unit
              }
            }
          }
        }

        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
    """

    variants = []

    after = None
    page = 0

    print("Reading Shopify catalogue...")

    while True:

        payload = graphql(
            store,
            token,
            query,
            {
                "first": 250,
                "after": after,
            },
        )

        connection = payload["data"]["productVariants"]

        variants.extend(connection["nodes"])

        page += 1

        if page % 20 == 0 or not connection["pageInfo"]["hasNextPage"]:
            print(f"  Shopify variants read: {len(variants):,}")

        if not connection["pageInfo"]["hasNextPage"]:
            break

        after = connection["pageInfo"]["endCursor"]

    return variants


# ---------------------------------------------------------
# B2B DISCOUNT RULE
# ---------------------------------------------------------

def calculate_discount_price(
    retail_price: D,
    vendor: str,
    handle: str,
) -> tuple[D, str, D]:

    vendor_key = normalize(vendor)
    handle_key = normalize(handle)

    # -----------------------------------------------------
    # NO DISCOUNT
    # -----------------------------------------------------

    # Link ECU Strada / AIM products
    if (
        vendor_key == "link ecu"
        and ("strada" in handle_key or "aim" in handle_key)
    ):
        discount = D("0")
        rule = "LINK ECU STRADA/AIM - NO DISCOUNT"

    elif vendor_key == "haltech":
        discount = D("0")
        rule = "HALTECH - NO DISCOUNT"

    elif vendor_key == "plazmaman":
        discount = D("0")
        rule = "PLAZMAMAN - NO DISCOUNT"

    # -----------------------------------------------------
    # 20% OFF
    # -----------------------------------------------------

    elif vendor_key == "proflow":
        discount = D("0.20")
        rule = "PROFLOW - 20%"

    elif vendor_key == "aeroflow":
        discount = D("0.20")
        rule = "AEROFLOW - 20%"

    # -----------------------------------------------------
    # 10% OFF
    # -----------------------------------------------------

    elif vendor_key == "link ecu":
        discount = D("0.10")
        rule = "LINK ECU - 10%"

    elif vendor_key == "rts":
        discount = D("0.10")
        rule = "RTS - 10%"

    elif vendor_key == "streetpro":
        discount = D("0.10")
        rule = "STREETPRO - 10%"

    elif vendor_key == "fast lane spares":
        discount = D("0.10")
        rule = "FAST LANE SPARES - 10%"

    # -----------------------------------------------------
    # 5% OFF
    # -----------------------------------------------------

    elif vendor_key in {
        "omp",
        "bell",
        "franklin performance",
        "pulsar",
        "turbosmart",
        "artec",
        "6boost",
        "cooper cobra",
        "winters performance",
        "kelford cams",
        "hughes race built",
        "drews automotive",
    }:
        discount = D("0.05")
        rule = f"{vendor.upper()} - 5%"

    # -----------------------------------------------------
    # STANDARD FALLBACK
    # -----------------------------------------------------

    elif retail_price > D("10000"):
        discount = D("0.02")
        rule = "OVER $10,000 - 2%"

    elif retail_price > D("5000"):
        discount = D("0.03")
        rule = "$5,000-$10,000 - 3%"

    else:
        discount = D("0.10")
        rule = "STANDARD - 10%"

    discount_price = round_1(
        retail_price * (D("1") - discount)
    )

    return discount_price, rule, discount


# ---------------------------------------------------------
# B2B PRICE CALCULATION
# ---------------------------------------------------------

def calculate_b2b_price(variant: dict) -> dict:

    product = variant.get("product") or {}
    inventory_item = variant.get("inventoryItem") or {}

    sku = (variant.get("sku") or "").strip()

    retail_price = dec(variant.get("price"))

    vendor = product.get("vendor") or ""
    handle = product.get("handle") or ""
    title = product.get("title") or ""

    tags = product.get("tags") or []

    cost_data = inventory_item.get("unitCost") or {}
    cost = dec(cost_data.get("amount"))

    measurement = inventory_item.get("measurement") or {}
    weight_data = measurement.get("weight") or {}
    weight_kg = dec(weight_data.get("value"))

    # -----------------------------------------------------
    # VALIDATION
    # -----------------------------------------------------

    if not sku:
        return {
            "status": "SKIPPED",
            "reason": "MISSING_SKU",
        }

    if retail_price is None or retail_price <= 0:
        return {
            "status": "SKIPPED",
            "reason": "INVALID_RETAIL_PRICE",
        }

    # -----------------------------------------------------
    # BRAND / STANDARD DISCOUNT
    # -----------------------------------------------------

    discount_price, discount_rule, discount = calculate_discount_price(
        retail_price,
        vendor,
        handle,
    )

    # -----------------------------------------------------
    # HOLLEY DIRECT TAG
    # -----------------------------------------------------

    tag_keys = {
        normalize(tag)
        for tag in tags
    }

    holley_direct = "holley direct" in tag_keys

    freight_rate = (
        HOLLEY_DIRECT_FREIGHT_PER_KG
        if holley_direct
        else NORMAL_FREIGHT_PER_KG
    )

    # -----------------------------------------------------
    # NO COST
    # -----------------------------------------------------

    # Matches the Excel behaviour:
    # If Cost is blank, no margin-floor calculation is possible,
    # therefore B2B price is based on the discount rule.

    if cost is None:

        return {
            "status": "CALCULATED",
            "reason": "NO_COST - DISCOUNT_ONLY",
            "sku": sku,
            "product": title,
            "handle": handle,
            "vendor": vendor,
            "tags": ", ".join(tags),
            "retail_price": retail_price,
            "cost": None,
            "weight_kg": weight_kg,
            "holley_direct": holley_direct,
            "freight_rate": freight_rate,
            "freight": None,
            "landed_cost": None,
            "discount_pct": discount,
            "discount_rule": discount_rule,
            "discount_price": discount_price,
            "margin_floor_pct": None,
            "margin_floor_price": None,
            "b2b_price": min(retail_price, discount_price),
            "actual_margin": None,
        }

    # -----------------------------------------------------
    # FREIGHT / LANDED COST
    # -----------------------------------------------------

    # If weight is missing, treat freight as zero for the dry run,
    # but clearly flag it in the audit.

    if weight_kg is None:
        freight = D("0")
        weight_missing = True
    else:
        freight = round_1(weight_kg * freight_rate)
        weight_missing = False

    landed_cost = round_1(cost + freight)

    # -----------------------------------------------------
    # MARGIN FLOOR
    # -----------------------------------------------------

    if landed_cost > MARGIN_BREAKPOINT:

        margin_floor_pct = HIGH_COST_MARGIN

        margin_floor_price = round_1(
            landed_cost / (D("1") - HIGH_COST_MARGIN)
        )

    else:

        margin_floor_pct = LOW_COST_MARGIN

        margin_floor_price = round_1(
            landed_cost / (D("1") - LOW_COST_MARGIN)
        )

    # -----------------------------------------------------
    # FINAL B2B PRICE
    #
    # Excel:
    # MIN(Retail Price, MAX(Discount Price, Margin Floor))
    # -----------------------------------------------------

    b2b_price = round_1(
        min(
            retail_price,
            max(
                discount_price,
                margin_floor_price,
            ),
        )
    )

    actual_margin = None

    if b2b_price > 0:
        actual_margin = (
            b2b_price - landed_cost
        ) / b2b_price

    reason = (
        "MISSING_WEIGHT - FREIGHT SET TO $0 FOR REVIEW"
        if weight_missing
        else "OK"
    )

    return {
        "status": "CALCULATED",
        "reason": reason,
        "sku": sku,
        "product": title,
        "handle": handle,
        "vendor": vendor,
        "tags": ", ".join(tags),
        "retail_price": retail_price,
        "cost": cost,
        "weight_kg": weight_kg,
        "holley_direct": holley_direct,
        "freight_rate": freight_rate,
        "freight": freight,
        "landed_cost": landed_cost,
        "discount_pct": discount,
        "discount_rule": discount_rule,
        "discount_price": discount_price,
        "margin_floor_pct": margin_floor_pct,
        "margin_floor_price": margin_floor_price,
        "b2b_price": b2b_price,
        "actual_margin": actual_margin,
    }


# ---------------------------------------------------------
# REPORT
# ---------------------------------------------------------

def create_report(variants: list[dict]):

    report_rows = []

    counts = {
        "shopify_variants": len(variants),
        "calculated": 0,
        "missing_sku": 0,
        "invalid_price": 0,
        "missing_cost": 0,
        "missing_weight": 0,
        "holley_direct": 0,
    }

    for variant in variants:

        result = calculate_b2b_price(variant)

        if result["status"] != "CALCULATED":

            reason = result.get("reason", "")

            if reason == "MISSING_SKU":
                counts["missing_sku"] += 1

            elif reason == "INVALID_RETAIL_PRICE":
                counts["invalid_price"] += 1

            continue

        counts["calculated"] += 1

        if result["cost"] is None:
            counts["missing_cost"] += 1

        if result["weight_kg"] is None:
            counts["missing_weight"] += 1

        if result["holley_direct"]:
            counts["holley_direct"] += 1

        report_rows.append({
            "sku": result["sku"],
            "product": result["product"],
            "handle": result["handle"],
            "vendor": result["vendor"],
            "tags": result["tags"],

            "retail_price": money(result["retail_price"]),
            "shopify_cost": money(result["cost"]),
            "weight_kg": (
                ""
                if result["weight_kg"] is None
                else str(result["weight_kg"])
            ),

            "holley_direct": (
                "YES"
                if result["holley_direct"]
                else "NO"
            ),

            "freight_rate_per_kg": money(
                result["freight_rate"]
            ),

            "freight": money(result["freight"]),
            "landed_cost": money(result["landed_cost"]),

            "discount_rule": result["discount_rule"],
            "discount_pct": pct(result["discount_pct"]),
            "discount_price": money(
                result["discount_price"]
            ),

            "margin_floor_pct": pct(
                result["margin_floor_pct"]
            ),

            "margin_floor_price": money(
                result["margin_floor_price"]
            ),

            "calculated_b2b_price": money(
                result["b2b_price"]
            ),

            "actual_margin": pct(
                result["actual_margin"]
            ),

            "note": result["reason"],
        })

    report_path = OUTPUT_DIR / "b2b_pricing_dry_run.csv"

    fieldnames = [
        "sku",
        "product",
        "handle",
        "vendor",
        "tags",
        "retail_price",
        "shopify_cost",
        "weight_kg",
        "holley_direct",
        "freight_rate_per_kg",
        "freight",
        "landed_cost",
        "discount_rule",
        "discount_pct",
        "discount_price",
        "margin_floor_pct",
        "margin_floor_price",
        "calculated_b2b_price",
        "actual_margin",
        "note",
    ]

    with report_path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(report_rows)

    summary_path = OUTPUT_DIR / "b2b_dry_run_summary.json"

    summary = {
        **counts,
        "mode": "B2B_DRY_RUN_READ_ONLY",
        "shopify_changes": 0,
        "report": str(report_path),
    }

    summary_path.write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    print()
    print("B2B DRY RUN COMPLETE")
    print("NO SHOPIFY DATA WAS CHANGED.")
    print()
    print(json.dumps(summary, indent=2))

    return report_path


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():

    print("FAST LANE SPARES")
    print("B2B PRICING DRY RUN")
    print("READ ONLY - NO SHOPIFY WRITES")
    print()

    store = required_env("SHOPIFY_STORE")
    client_id = required_env("SHOPIFY_CLIENT_ID")
    client_secret = required_env("SHOPIFY_CLIENT_SECRET")

    token = get_shopify_token(
        store,
        client_id,
        client_secret,
    )

    variants = fetch_shopify_variants(
        store,
        token,
    )

    create_report(variants)


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:
        print(
            f"\nFATAL ERROR: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
