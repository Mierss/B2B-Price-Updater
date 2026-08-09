#!/usr/bin/env python3
"""
Fast Lane Spares - B2B Pricing LIVE

Standalone live updater.

- Reads Shopify variants
- Reads B2B pricing rules from b2b_settings.json
- Finds the configured Shopify native B2B catalog
- Reads existing fixed B2B prices
- Calculates the required B2B price
- Updates ONLY changed/new fixed prices
- Generates audit CSV + JSON summary
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
# BASIC SETTINGS
# ---------------------------------------------------------

API_VERSION = os.getenv(
    "SHOPIFY_API_VERSION",
    "2026-07",
)

OUTPUT_DIR = Path(
    os.getenv(
        "OUTPUT_DIR",
        "output",
    )
)

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

SETTINGS_PATH = Path(
    os.getenv(
        "B2B_SETTINGS_FILE",
        "b2b_settings.json",
    )
)

D = Decimal

PRICE_TOLERANCE = D("0.05")

# Keep writes in manageable groups.
BATCH_SIZE = 200


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def dec(value, default=None):
    if value is None:
        return default

    text = str(value).strip()

    if not text:
        return default

    try:
        return D(
            text.replace(",", "")
        )
    except InvalidOperation:
        return default


def normalize(value: str) -> str:
    return (
        value or ""
    ).strip().lower()


def round_1(value: D) -> D:
    return value.quantize(
        D("0.1"),
        rounding=ROUND_HALF_UP,
    )


def money(value) -> str:
    if value is None:
        return ""

    return f"{value:.2f}"


def pct(value) -> str:
    if value is None:
        return ""

    return (
        f"{value * D('100'):.2f}%"
    )


def chunked(items, size):
    for index in range(
        0,
        len(items),
        size,
    ):
        yield items[
            index:index + size
        ]


# ---------------------------------------------------------
# LOAD SETTINGS
# ---------------------------------------------------------

def load_settings():
    if not SETTINGS_PATH.exists():
        raise RuntimeError(
            f"B2B settings file not found: "
            f"{SETTINGS_PATH}"
        )

    settings = json.loads(
        SETTINGS_PATH.read_text(
            encoding="utf-8",
        )
    )

    required_sections = [
        "catalog_title",
        "freight",
        "margin_floors",
        "brand_discounts",
        "special_rules",
        "fallback_discounts",
    ]

    for section in required_sections:
        if section not in settings:
            raise RuntimeError(
                f"Missing B2B settings section: "
                f"{section}"
            )

    return settings


SETTINGS = load_settings()

CATALOG_TITLE = (
    SETTINGS["catalog_title"]
)

NORMAL_FREIGHT_PER_KG = D(
    str(
        SETTINGS[
            "freight"
        ][
            "default_per_kg"
        ]
    )
)

HOLLEY_DIRECT_FREIGHT_PER_KG = D(
    str(
        SETTINGS[
            "freight"
        ][
            "holley_direct_per_kg"
        ]
    )
)

BRAND_DISCOUNTS = {
    normalize(brand):
        D(str(discount))
    for brand, discount
    in SETTINGS[
        "brand_discounts"
    ].items()
}

LINK_ECU_STRADA_AIM_DISCOUNT = D(
    str(
        SETTINGS[
            "special_rules"
        ][
            "link_ecu_strada_aim_discount"
        ]
    )
)

FALLBACK_DISCOUNTS = sorted(
    SETTINGS[
        "fallback_discounts"
    ],
    key=lambda row: D(
        str(
            row[
                "min_retail_price"
            ]
        )
    ),
    reverse=True,
)


# ---------------------------------------------------------
# SHOPIFY AUTH
# ---------------------------------------------------------

def get_shopify_token(
    store: str,
    client_id: str,
    client_secret: str,
) -> str:

    url = (
        f"https://{store}"
        f"/admin/oauth/access_token"
    )

    response = requests.post(
        url,
        data={
            "grant_type":
                "client_credentials",
            "client_id":
                client_id,
            "client_secret":
                client_secret,
        },
        headers={
            "Content-Type":
                "application/x-www-form-urlencoded"
        },
        timeout=60,
    )

    response.raise_for_status()

    payload = response.json()

    token = payload.get(
        "access_token"
    )

    if not token:
        raise RuntimeError(
            "Shopify token response "
            "did not include access_token"
        )

    print(
        "Shopify authentication OK."
    )

    return token


# ---------------------------------------------------------
# GRAPHQL
# ---------------------------------------------------------

def graphql(
    store: str,
    token: str,
    query: str,
    variables: dict,
) -> dict:

    url = (
        f"https://{store}"
        f"/admin/api/"
        f"{API_VERSION}"
        f"/graphql.json"
    )

    while True:

        response = requests.post(
            url,
            headers={
                "Content-Type":
                    "application/json",
                "X-Shopify-Access-Token":
                    token,
            },
            json={
                "query":
                    query,
                "variables":
                    variables,
            },
            timeout=90,
        )

        if response.status_code in (
            429,
            500,
            502,
            503,
            504,
        ):

            wait = int(
                response.headers.get(
                    "Retry-After",
                    "3",
                )
            )

            print(
                f"Shopify HTTP "
                f"{response.status_code}; "
                f"retrying in {wait}s..."
            )

            time.sleep(wait)

            continue

        response.raise_for_status()

        payload = response.json()

        errors = (
            payload.get("errors")
            or []
        )

        if errors:

            throttled = any(
                (
                    error.get(
                        "extensions"
                    )
                    or {}
                ).get(
                    "code"
                )
                == "THROTTLED"
                for error in errors
            )

            if throttled:

                print(
                    "Shopify GraphQL "
                    "throttled; "
                    "retrying in 5s..."
                )

                time.sleep(5)

                continue

            raise RuntimeError(
                json.dumps(
                    errors,
                    indent=2,
                )
            )

        return payload


# ---------------------------------------------------------
# READ SHOPIFY VARIANTS
# ---------------------------------------------------------

def fetch_shopify_variants(
    store: str,
    token: str,
) -> list[dict]:

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

    print(
        "Reading Shopify catalogue..."
    )

    while True:

        payload = graphql(
            store,
            token,
            query,
            {
                "first":
                    250,
                "after":
                    after,
            },
        )

        connection = (
            payload[
                "data"
            ][
                "productVariants"
            ]
        )

        variants.extend(
            connection[
                "nodes"
            ]
        )

        page += 1

        if (
            page % 20 == 0
            or not connection[
                "pageInfo"
            ][
                "hasNextPage"
            ]
        ):

            print(
                f"  Shopify variants read: "
                f"{len(variants):,}"
            )

        if not connection[
            "pageInfo"
        ][
            "hasNextPage"
        ]:
            break

        after = connection[
            "pageInfo"
        ][
            "endCursor"
        ]

    return variants


# ---------------------------------------------------------
# FIND PRICE LIST
# ---------------------------------------------------------

def find_b2b_price_list(
    store: str,
    token: str,
) -> dict:

    query = """
    query FastLanePriceLists(
      $first: Int!,
      $after: String
    ) {
      priceLists(
        first: $first,
        after: $after
      ) {
        nodes {
          id
          name
          currency
          fixedPricesCount

          catalog {
            id
            title
          }

          parent {
            adjustment {
              type
              value
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

    after = None

    matches = []

    print()
    print(
        f'Looking for Shopify catalog '
        f'"{CATALOG_TITLE}"...'
    )

    while True:

        payload = graphql(
            store,
            token,
            query,
            {
                "first":
                    100,
                "after":
                    after,
            },
        )

        connection = (
            payload[
                "data"
            ][
                "priceLists"
            ]
        )

        for price_list in (
            connection[
                "nodes"
            ]
        ):

            catalog = (
                price_list.get(
                    "catalog"
                )
                or {}
            )

            if (
                catalog.get(
                    "title"
                )
                == CATALOG_TITLE
            ):

                matches.append(
                    price_list
                )

        if not connection[
            "pageInfo"
        ][
            "hasNextPage"
        ]:
            break

        after = connection[
            "pageInfo"
        ][
            "endCursor"
        ]

    if not matches:

        raise RuntimeError(
            f'Could not find price list '
            f'for catalog '
            f'"{CATALOG_TITLE}".'
        )

    if len(matches) > 1:

        raise RuntimeError(
            f'Found {len(matches)} '
            f'price lists for '
            f'"{CATALOG_TITLE}".'
        )

    price_list = matches[0]

    catalog = (
        price_list.get(
            "catalog"
        )
        or {}
    )

    print(
        "Catalog found."
    )

    print(
        f"  Catalog: "
        f"{catalog.get('title')}"
    )

    print(
        f"  Price List ID: "
        f"{price_list.get('id')}"
    )

    print(
        f"  Currency: "
        f"{price_list.get('currency')}"
    )

    print(
        f"  Existing fixed prices: "
        f"{price_list.get('fixedPricesCount')}"
    )

    return price_list


# ---------------------------------------------------------
# READ CURRENT PRICE LIST PRICES
# ---------------------------------------------------------

def fetch_price_list_prices(
    store: str,
    token: str,
    price_list_id: str,
) -> dict[str, dict]:

    query = """
    query FastLaneB2BPriceListPrices(
      $id: ID!,
      $first: Int!,
      $after: String
    ) {
      priceList(id: $id) {
        id

        prices(
          first: $first,
          after: $after
        ) {
          nodes {
            originType

            price {
              amount
              currencyCode
            }

            variant {
              id
            }
          }

          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    }
    """

    prices = {}

    after = None

    page = 0

    print()
    print(
        "Reading current B2B prices..."
    )

    while True:

        payload = graphql(
            store,
            token,
            query,
            {
                "id":
                    price_list_id,
                "first":
                    250,
                "after":
                    after,
            },
        )

        price_list = (
            payload[
                "data"
            ][
                "priceList"
            ]
        )

        if not price_list:
            raise RuntimeError(
                "Price list not found"
            )

        connection = (
            price_list[
                "prices"
            ]
        )

        for node in (
            connection[
                "nodes"
            ]
        ):

            variant = (
                node.get(
                    "variant"
                )
                or {}
            )

            variant_id = (
                variant.get(
                    "id"
                )
            )

            amount = dec(
                (
                    node.get(
                        "price"
                    )
                    or {}
                ).get(
                    "amount"
                )
            )

            if (
                variant_id
                and amount
                is not None
            ):

                prices[
                    variant_id
                ] = {
                    "price":
                        amount,
                    "origin_type":
                        (
                            node.get(
                                "originType"
                            )
                            or ""
                        ),
                }

        page += 1

        if (
            page % 20 == 0
            or not connection[
                "pageInfo"
            ][
                "hasNextPage"
            ]
        ):

            print(
                f"  B2B prices read: "
                f"{len(prices):,}"
            )

        if not connection[
            "pageInfo"
        ][
            "hasNextPage"
        ]:
            break

        after = connection[
            "pageInfo"
        ][
            "endCursor"
        ]

    return prices


# ---------------------------------------------------------
# BRAND / FALLBACK DISCOUNT
# ---------------------------------------------------------

def calculate_discount_price(
    retail_price: D,
    vendor: str,
    handle: str,
):

    vendor_key = normalize(
        vendor
    )

    handle_key = normalize(
        handle
    )

    if (
        vendor_key
        == "link ecu"
        and (
            "strada"
            in handle_key
            or "aim"
            in handle_key
        )
    ):

        discount = (
            LINK_ECU_STRADA_AIM_DISCOUNT
        )

        rule = (
            "LINK ECU STRADA/AIM"
        )

    elif (
        vendor_key
        in BRAND_DISCOUNTS
    ):

        discount = (
            BRAND_DISCOUNTS[
                vendor_key
            ]
        )

        rule = (
            f"BRAND: {vendor}"
        )

    else:

        discount = None

        rule = None

        for row in (
            FALLBACK_DISCOUNTS
        ):

            minimum = D(
                str(
                    row[
                        "min_retail_price"
                    ]
                )
            )

            if (
                retail_price
                > minimum
                or minimum
                == D("0")
            ):

                discount = D(
                    str(
                        row[
                            "discount"
                        ]
                    )
                )

                rule = (
                    f"FALLBACK "
                    f"{discount * D('100')}%"
                )

                break

        if discount is None:
            raise RuntimeError(
                "No fallback "
                "discount matched"
            )

    discount_price = round_1(
        retail_price
        * (
            D("1")
            - discount
        )
    )

    return (
        discount_price,
        rule,
        discount,
    )


# ---------------------------------------------------------
# MARGIN FLOOR
# ---------------------------------------------------------

def get_margin_floor(
    landed_cost: D,
) -> D:

    for tier in (
        SETTINGS[
            "margin_floors"
        ]
    ):

        max_cost = (
            tier[
                "max_landed_cost"
            ]
        )

        margin = D(
            str(
                tier[
                    "margin"
                ]
            )
        )

        if (
            max_cost is None
            or landed_cost
            <= D(
                str(max_cost)
            )
        ):

            return margin

    raise RuntimeError(
        "No B2B margin floor matched"
    )


# ---------------------------------------------------------
# CALCULATE B2B PRICE
# ---------------------------------------------------------

def calculate_b2b_price(
    variant: dict,
):

    product = (
        variant.get(
            "product"
        )
        or {}
    )

    inventory_item = (
        variant.get(
            "inventoryItem"
        )
        or {}
    )

    sku = (
        variant.get(
            "sku"
        )
        or ""
    ).strip()

    retail_price = dec(
        variant.get(
            "price"
        )
    )

    if not sku:

        return {
            "status":
                "SKIPPED",
            "reason":
                "MISSING_SKU",
        }

    if (
        retail_price is None
        or retail_price <= 0
    ):

        return {
            "status":
                "SKIPPED",
            "reason":
                "INVALID_RETAIL_PRICE",
        }

    vendor = (
        product.get(
            "vendor"
        )
        or ""
    )

    handle = (
        product.get(
            "handle"
        )
        or ""
    )

    title = (
        product.get(
            "title"
        )
        or ""
    )

    tags = (
        product.get(
            "tags"
        )
        or []
    )

    cost = dec(
        (
            inventory_item.get(
                "unitCost"
            )
            or {}
        ).get(
            "amount"
        )
    )

    measurement = (
        inventory_item.get(
            "measurement"
        )
        or {}
    )

    weight_kg = dec(
        (
            measurement.get(
                "weight"
            )
            or {}
        ).get(
            "value"
        )
    )

    (
        discount_price,
        discount_rule,
        discount,
    ) = calculate_discount_price(
        retail_price,
        vendor,
        handle,
    )

    tag_keys = {
        normalize(tag)
        for tag in tags
    }

    holley_direct = (
        "holley direct"
        in tag_keys
    )

    freight_rate = (
        HOLLEY_DIRECT_FREIGHT_PER_KG
        if holley_direct
        else NORMAL_FREIGHT_PER_KG
    )

    # No cost:
    # discount-only price.

    if cost is None:

        b2b_price = round_1(
            min(
                retail_price,
                discount_price,
            )
        )

        return {
            "status":
                "CALCULATED",

            "sku":
                sku,

            "variant_id":
                variant.get(
                    "id"
                ),

            "product":
                title,

            "vendor":
                vendor,

            "retail_price":
                retail_price,

            "cost":
                None,

            "weight_kg":
                weight_kg,

            "freight":
                None,

            "landed_cost":
                None,

            "discount_rule":
                discount_rule,

            "discount_pct":
                discount,

            "margin_floor_price":
                None,

            "b2b_price":
                b2b_price,

            "note":
                "NO_COST - DISCOUNT_ONLY",
        }

    if weight_kg is None:

        freight = D("0")

        note = (
            "MISSING_WEIGHT - "
            "FREIGHT SET TO $0"
        )

    else:

        freight = round_1(
            weight_kg
            * freight_rate
        )

        note = "OK"

    landed_cost = round_1(
        cost
        + freight
    )

    margin_floor_pct = (
        get_margin_floor(
            landed_cost
        )
    )

    margin_floor_price = round_1(
        landed_cost
        / (
            D("1")
            - margin_floor_pct
        )
    )

    b2b_price = round_1(
        min(
            retail_price,
            max(
                discount_price,
                margin_floor_price,
            ),
        )
    )

    return {
        "status":
            "CALCULATED",

        "sku":
            sku,

        "variant_id":
            variant.get(
                "id"
            ),

        "product":
            title,

        "vendor":
            vendor,

        "retail_price":
            retail_price,

        "cost":
            cost,

        "weight_kg":
            weight_kg,

        "freight":
            freight,

        "landed_cost":
            landed_cost,

        "discount_rule":
            discount_rule,

        "discount_pct":
            discount,

        "margin_floor_price":
            margin_floor_price,

        "b2b_price":
            b2b_price,

        "note":
            note,
    }


# ---------------------------------------------------------
# WRITE FIXED PRICES
# ---------------------------------------------------------

def update_fixed_prices(
    store: str,
    token: str,
    price_list_id: str,
    currency: str,
    rows: list[dict],
):

    mutation = """
    mutation FastLaneB2BPriceUpdate(
      $priceListId: ID!,
      $prices: [PriceListPriceInput!]!
    ) {
      priceListFixedPricesAdd(
        priceListId: $priceListId,
        prices: $prices
      ) {
        prices {
          variant {
            id
          }

          price {
            amount
            currencyCode
          }
        }

        userErrors {
          field
          code
          message
        }
      }
    }
    """

    prices = []

    for row in rows:

        prices.append({
            "variantId":
                row[
                    "variant_id"
                ],

            "price": {
                "amount":
                    str(
                        row[
                            "new_b2b_price"
                        ]
                    ),

                "currencyCode":
                    currency,
            },
        })

    payload = graphql(
        store,
        token,
        mutation,
        {
            "priceListId":
                price_list_id,

            "prices":
                prices,
        },
    )

    result = (
        payload[
            "data"
        ][
            "priceListFixedPricesAdd"
        ]
    )

    user_errors = (
        result.get(
            "userErrors"
        )
        or []
    )

    if user_errors:

        raise RuntimeError(
            json.dumps(
                user_errors,
                indent=2,
            )
        )

    return (
        result.get(
            "prices"
        )
        or []
    )


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():

    print(
        "FAST LANE SPARES"
    )

    print(
        "B2B PRICING LIVE"
    )

    print()

    print(
        f"Target catalog: "
        f"{CATALOG_TITLE}"
    )

    store = required_env(
        "SHOPIFY_STORE"
    )

    client_id = required_env(
        "SHOPIFY_CLIENT_ID"
    )

    client_secret = required_env(
        "SHOPIFY_CLIENT_SECRET"
    )

    token = (
        get_shopify_token(
            store,
            client_id,
            client_secret,
        )
    )

    variants = (
        fetch_shopify_variants(
            store,
            token,
        )
    )

    price_list = (
        find_b2b_price_list(
            store,
            token,
        )
    )

    price_list_id = (
        price_list[
            "id"
        ]
    )

    currency = (
        price_list.get(
            "currency"
        )
        or "NZD"
    )

    existing_prices = (
        fetch_price_list_prices(
            store,
            token,
            price_list_id,
        )
    )

    candidates = []

    skipped = []

    unchanged = 0

    for variant in variants:

        result = (
            calculate_b2b_price(
                variant
            )
        )

        if (
            result[
                "status"
            ]
            != "CALCULATED"
        ):

            skipped.append({
                "sku":
                    (
                        variant.get(
                            "sku"
                        )
                        or ""
                    ),

                "variant_id":
                    variant.get(
                        "id"
                    ),

                "reason":
                    result.get(
                        "reason",
                        "SKIPPED",
                    ),
            })

            continue

        variant_id = (
            result[
                "variant_id"
            ]
        )

        current = (
            existing_prices.get(
                variant_id
            )
        )

        current_price = (
            current.get(
                "price"
            )
            if current
            else None
        )

        current_origin = (
            current.get(
                "origin_type"
            )
            if current
            else "NONE"
        )

        calculated_price = (
            result[
                "b2b_price"
            ]
        )

        if (
            current_price
            is not None
            and abs(
                calculated_price
                - current_price
            )
            <= PRICE_TOLERANCE
        ):

            unchanged += 1

            continue

        difference = (
            calculated_price
            - current_price
            if current_price
            is not None
            else None
        )

        candidates.append({

            "sku":
                result[
                    "sku"
                ],

            "product":
                result[
                    "product"
                ],

            "vendor":
                result[
                    "vendor"
                ],

            "variant_id":
                variant_id,

            "retail_price":
                result[
                    "retail_price"
                ],

            "old_b2b_price":
                current_price,

            "old_price_source":
                current_origin,

            "new_b2b_price":
                calculated_price,

            "difference":
                difference,

            "discount_rule":
                result[
                    "discount_rule"
                ],

            "landed_cost":
                result[
                    "landed_cost"
                ],

            "margin_floor":
                result[
                    "margin_floor_price"
                ],

            "note":
                result[
                    "note"
                ],

            "status":
                "PENDING",

            "error":
                "",
        })

    print()
    print(
        f"Shopify variants: "
        f"{len(variants):,}"
    )

    print(
        f"Unchanged: "
        f"{unchanged:,}"
    )

    print(
        f"Prices to write: "
        f"{len(candidates):,}"
    )

    print(
        f"Skipped: "
        f"{len(skipped):,}"
    )

    print()

    successful = 0

    failed = 0

    total_batches = (
        (
            len(candidates)
            + BATCH_SIZE
            - 1
        )
        // BATCH_SIZE
    )

    for batch_number, batch in enumerate(
        chunked(
            candidates,
            BATCH_SIZE,
        ),
        start=1,
    ):

        print(
            f"Writing batch "
            f"{batch_number}/"
            f"{total_batches} "
            f"({len(batch)} prices)..."
        )

        try:

            returned = (
                update_fixed_prices(
                    store,
                    token,
                    price_list_id,
                    currency,
                    batch,
                )
            )

            for row in batch:
                row[
                    "status"
                ] = "SUCCESS"

            successful += (
                len(batch)
            )

            print(
                f"  SUCCESS "
                f"({len(returned)} prices "
                f"returned by Shopify)"
            )

        except Exception as exc:

            failed += (
                len(batch)
            )

            for row in batch:

                row[
                    "status"
                ] = "ERROR"

                row[
                    "error"
                ] = str(exc)

            print(
                f"  ERROR: "
                f"{exc}",
                file=sys.stderr,
            )

    # -----------------------------------------------------
    # AUDIT CSV
    # -----------------------------------------------------

    audit_path = (
        OUTPUT_DIR
        / "b2b_live_update_audit.csv"
    )

    fields = [
        "sku",
        "product",
        "vendor",
        "variant_id",
        "retail_price",
        "old_b2b_price",
        "old_price_source",
        "new_b2b_price",
        "difference",
        "discount_rule",
        "landed_cost",
        "margin_floor",
        "note",
        "status",
        "error",
    ]

    with audit_path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields,
        )

        writer.writeheader()

        for row in candidates:

            output = dict(
                row
            )

            for key in (
                "retail_price",
                "old_b2b_price",
                "new_b2b_price",
                "difference",
                "landed_cost",
                "margin_floor",
            ):

                output[
                    key
                ] = money(
                    row[
                        key
                    ]
                )

            writer.writerow(
                output
            )

    # -----------------------------------------------------
    # SKIPPED CSV
    # -----------------------------------------------------

    skipped_path = (
        OUTPUT_DIR
        / "b2b_live_skipped.csv"
    )

    with skipped_path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=[
                "sku",
                "variant_id",
                "reason",
            ],
        )

        writer.writeheader()

        writer.writerows(
            skipped
        )

    # -----------------------------------------------------
    # SUMMARY
    # -----------------------------------------------------

    summary = {

        "catalog_title":
            CATALOG_TITLE,

        "price_list_id":
            price_list_id,

        "currency":
            currency,

        "shopify_variants":
            len(variants),

        "unchanged":
            unchanged,

        "attempted_updates":
            len(candidates),

        "successful":
            successful,

        "failed":
            failed,

        "skipped":
            len(skipped),

        "audit_file":
            str(
                audit_path
            ),

        "mode":
            "B2B_LIVE",
    }

    summary_path = (
        OUTPUT_DIR
        / "b2b_live_summary.json"
    )

    summary_path.write_text(
        json.dumps(
            summary,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print(
        "B2B LIVE UPDATE COMPLETE"
    )

    print(
        json.dumps(
            summary,
            indent=2,
        )
    )

    if failed:
        sys.exit(1)


if __name__ == "__main__":

    try:

        main()

    except Exception as exc:

        print(
            f"\nFATAL ERROR: "
            f"{exc}",
            file=sys.stderr,
        )

        sys.exit(1)
