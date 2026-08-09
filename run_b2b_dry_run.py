#!/usr/bin/env python3
"""
Fast Lane Spares - B2B Pricing DRY RUN

READ ONLY:
- Reads Shopify products and variants.
- Finds the native Shopify B2B catalog from b2b_settings.json.
- Reads its existing fixed prices and default relative adjustment.
- Calculates proposed B2B prices from configurable rules.
- Compares current B2B pricing against calculated B2B pricing.
- DOES NOT write or change anything in Shopify.
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

API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-07")

OUTPUT_DIR = Path(
    os.getenv("OUTPUT_DIR", "output")
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

PRICE_TOLERANCE = Decimal("0.05")

D = Decimal


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

    s = str(value).strip()

    if not s:
        return default

    try:
        return D(
            s.replace(",", "")
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


# ---------------------------------------------------------
# LOAD B2B SETTINGS
# ---------------------------------------------------------

def load_settings():
    if not SETTINGS_PATH.exists():
        raise RuntimeError(
            f"B2B settings file not found: "
            f"{SETTINGS_PATH}"
        )

    settings = json.loads(
        SETTINGS_PATH.read_text(
            encoding="utf-8"
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
        SETTINGS["freight"][
            "default_per_kg"
        ]
    )
)

HOLLEY_DIRECT_FREIGHT_PER_KG = D(
    str(
        SETTINGS["freight"][
            "holley_direct_per_kg"
        ]
    )
)

BRAND_DISCOUNTS = {
    normalize(brand): D(
        str(discount)
    )
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
# SHOPIFY AUTHENTICATION
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
# SHOPIFY GRAPHQL
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
                "query": query,
                "variables": variables,
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
                ) == "THROTTLED"
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
# READ SHOPIFY CATALOGUE
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
                "first": 250,
                "after": after,
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
# FIND SHOPIFY B2B PRICE LIST
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
                "first": 100,
                "after": after,
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
            connection["nodes"]
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
            f'Could not find a Shopify '
            f'price list attached to '
            f'catalog "{CATALOG_TITLE}".'
        )

    if len(matches) > 1:

        raise RuntimeError(
            f'Found {len(matches)} '
            f'price lists attached to '
            f'catalog "{CATALOG_TITLE}". '
            f'Expected exactly one.'
        )

    price_list = matches[0]

    catalog = (
        price_list.get(
            "catalog"
        )
        or {}
    )

    parent = (
        price_list.get(
            "parent"
        )
        or {}
    )

    adjustment = (
        parent.get(
            "adjustment"
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
        f"  Catalog ID: "
        f"{catalog.get('id')}"
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
        f"  Fixed prices: "
        f"{price_list.get('fixedPricesCount')}"
    )

    print(
        f"  Parent adjustment: "
        f"{adjustment.get('type')} "
        f"{adjustment.get('value')}"
    )

    return price_list


# ---------------------------------------------------------
# READ EXISTING FIXED B2B PRICES
# ---------------------------------------------------------

def fetch_fixed_b2b_prices(
    store: str,
    token: str,
    price_list_id: str,
) -> dict[str, D]:

    query = """
    query FastLaneFixedB2BPrices(
      $id: ID!,
      $first: Int!,
      $after: String
    ) {
      priceList(id: $id) {
        id
        name

        prices(
          first: $first,
          after: $after,
          originType: FIXED
        ) {
          nodes {
            price {
              amount
              currencyCode
            }

            originType

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
        "Reading existing fixed "
        "B2B prices..."
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
                f"Price list not found: "
                f"{price_list_id}"
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
                ] = amount

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
                f"  Fixed B2B prices read: "
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
# CURRENT RELATIVE B2B PRICE
# ---------------------------------------------------------

def calculate_relative_b2b_price(
    retail_price: D,
    adjustment: dict,
):

    if not adjustment:
        return None

    adjustment_type = (
        adjustment.get(
            "type"
        )
        or ""
    )

    adjustment_value = dec(
        adjustment.get(
            "value"
        )
    )

    if adjustment_value is None:
        return None

    fraction = (
        adjustment_value
        / D("100")
    )

    if (
        adjustment_type
        == "PERCENTAGE_DECREASE"
    ):

        return round_1(
            retail_price
            * (
                D("1")
                - fraction
            )
        )

    if (
        adjustment_type
        == "PERCENTAGE_INCREASE"
    ):

        return round_1(
            retail_price
            * (
                D("1")
                + fraction
            )
        )

    return None


# ---------------------------------------------------------
# B2B BRAND / FALLBACK DISCOUNT
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

    # Special Link ECU exception

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
            "LINK ECU STRADA/AIM "
            f"- "
            f"{discount * D('100')}%"
        )

    # Brand-specific override

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
            f"{vendor.upper()} - "
            f"{discount * D('100')}%"
        )

    # Standard fallback thresholds

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
                    f"FALLBACK - "
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

        max_cost = tier[
            "max_landed_cost"
        ]

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
# CALCULATE PROPOSED B2B PRICE
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

    # -----------------------------------------------------
    # NO COST = DISCOUNT ONLY
    # -----------------------------------------------------

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

            "reason":
                "NO_COST - DISCOUNT_ONLY",

            "sku":
                sku,

            "variant_id":
                variant.get("id"),

            "product":
                title,

            "handle":
                handle,

            "vendor":
                vendor,

            "tags":
                ", ".join(tags),

            "retail_price":
                retail_price,

            "cost":
                None,

            "weight_kg":
                weight_kg,

            "holley_direct":
                holley_direct,

            "freight_rate":
                freight_rate,

            "freight":
                None,

            "landed_cost":
                None,

            "discount_pct":
                discount,

            "discount_rule":
                discount_rule,

            "discount_price":
                discount_price,

            "margin_floor_pct":
                None,

            "margin_floor_price":
                None,

            "b2b_price":
                b2b_price,

            "actual_margin":
                None,
        }

    # -----------------------------------------------------
    # FREIGHT
    # -----------------------------------------------------

    if weight_kg is None:

        freight = D("0")

        weight_missing = True

    else:

        freight = round_1(
            weight_kg
            * freight_rate
        )

        weight_missing = False

    landed_cost = round_1(
        cost
        + freight
    )

    # -----------------------------------------------------
    # MARGIN FLOOR
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # FINAL B2B PRICE
    #
    # MIN(
    #   Retail,
    #   MAX(
    #     Discount Price,
    #     Margin Floor Price
    #   )
    # )
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
            b2b_price
            - landed_cost
        ) / b2b_price

    reason = (
        "MISSING_WEIGHT - "
        "FREIGHT SET TO $0 "
        "FOR REVIEW"
        if weight_missing
        else "OK"
    )

    return {
        "status":
            "CALCULATED",

        "reason":
            reason,

        "sku":
            sku,

        "variant_id":
            variant.get("id"),

        "product":
            title,

        "handle":
            handle,

        "vendor":
            vendor,

        "tags":
            ", ".join(tags),

        "retail_price":
            retail_price,

        "cost":
            cost,

        "weight_kg":
            weight_kg,

        "holley_direct":
            holley_direct,

        "freight_rate":
            freight_rate,

        "freight":
            freight,

        "landed_cost":
            landed_cost,

        "discount_pct":
            discount,

        "discount_rule":
            discount_rule,

        "discount_price":
            discount_price,

        "margin_floor_pct":
            margin_floor_pct,

        "margin_floor_price":
            margin_floor_price,

        "b2b_price":
            b2b_price,

        "actual_margin":
            actual_margin,
    }


# ---------------------------------------------------------
# CREATE COMPARISON REPORT
# ---------------------------------------------------------

def create_report(
    variants: list[dict],
    fixed_prices: dict[str, D],
    price_list: dict,
):

    parent = (
        price_list.get(
            "parent"
        )
        or {}
    )

    adjustment = (
        parent.get(
            "adjustment"
        )
        or {}
    )

    report_rows = []

    counts = {
        "shopify_variants":
            len(variants),

        "calculated":
            0,

        "missing_sku":
            0,

        "invalid_price":
            0,

        "missing_cost":
            0,

        "missing_weight":
            0,

        "holley_direct":
            0,

        "current_fixed_prices":
            len(fixed_prices),

        "no_change":
            0,

        "would_update":
            0,

        "would_add_fixed_price":
            0,
    }

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

            reason = (
                result.get(
                    "reason",
                    "",
                )
            )

            if (
                reason
                == "MISSING_SKU"
            ):

                counts[
                    "missing_sku"
                ] += 1

            elif (
                reason
                == "INVALID_RETAIL_PRICE"
            ):

                counts[
                    "invalid_price"
                ] += 1

            continue

        counts[
            "calculated"
        ] += 1

        if (
            result[
                "cost"
            ]
            is None
        ):

            counts[
                "missing_cost"
            ] += 1

        if (
            result[
                "weight_kg"
            ]
            is None
        ):

            counts[
                "missing_weight"
            ] += 1

        if (
            result[
                "holley_direct"
            ]
        ):

            counts[
                "holley_direct"
            ] += 1

        variant_id = (
            result[
                "variant_id"
            ]
        )

        fixed_price = (
            fixed_prices.get(
                variant_id
            )
        )

        relative_price = (
            calculate_relative_b2b_price(
                result[
                    "retail_price"
                ],
                adjustment,
            )
        )

        # Shopify uses fixed price first,
        # otherwise the price-list adjustment.

        if fixed_price is not None:

            current_b2b_price = (
                fixed_price
            )

            current_price_source = (
                "FIXED"
            )

        else:

            current_b2b_price = (
                relative_price
            )

            current_price_source = (
                "RELATIVE"
                if relative_price
                is not None
                else "NONE"
            )

        calculated_price = (
            result[
                "b2b_price"
            ]
        )

        difference = None

        if (
            current_b2b_price
            is not None
        ):

            difference = (
                calculated_price
                - current_b2b_price
            )

        # -------------------------------------------------
        # ACTION
        # -------------------------------------------------

        if (
            current_b2b_price
            is not None
            and abs(
                calculated_price
                - current_b2b_price
            )
            <= PRICE_TOLERANCE
        ):

            action = (
                "NO_CHANGE"
            )

            counts[
                "no_change"
            ] += 1

        elif fixed_price is not None:

            action = (
                "WOULD_UPDATE"
            )

            counts[
                "would_update"
            ] += 1

        else:

            action = (
                "WOULD_ADD_FIXED_PRICE"
            )

            counts[
                "would_add_fixed_price"
            ] += 1

        report_rows.append({

            "sku":
                result["sku"],

            "product":
                result["product"],

            "handle":
                result["handle"],

            "vendor":
                result["vendor"],

            "retail_price":
                money(
                    result[
                        "retail_price"
                    ]
                ),

            "current_b2b_price":
                money(
                    current_b2b_price
                ),

            "current_price_source":
                current_price_source,

            "current_fixed_price":
                money(
                    fixed_price
                ),

            "current_relative_price":
                money(
                    relative_price
                ),

            "calculated_b2b_price":
                money(
                    calculated_price
                ),

            "difference":
                money(
                    difference
                ),

            "action":
                action,

            "shopify_cost":
                money(
                    result[
                        "cost"
                    ]
                ),

            "weight_kg":
                (
                    ""
                    if result[
                        "weight_kg"
                    ]
                    is None
                    else str(
                        result[
                            "weight_kg"
                        ]
                    )
                ),

            "holley_direct":
                (
                    "YES"
                    if result[
                        "holley_direct"
                    ]
                    else "NO"
                ),

            "freight_rate_per_kg":
                money(
                    result[
                        "freight_rate"
                    ]
                ),

            "freight":
                money(
                    result[
                        "freight"
                    ]
                ),

            "landed_cost":
                money(
                    result[
                        "landed_cost"
                    ]
                ),

            "discount_rule":
                result[
                    "discount_rule"
                ],

            "discount_pct":
                pct(
                    result[
                        "discount_pct"
                    ]
                ),

            "discount_price":
                money(
                    result[
                        "discount_price"
                    ]
                ),

            "margin_floor_pct":
                pct(
                    result[
                        "margin_floor_pct"
                    ]
                ),

            "margin_floor_price":
                money(
                    result[
                        "margin_floor_price"
                    ]
                ),

            "actual_margin":
                pct(
                    result[
                        "actual_margin"
                    ]
                ),

            "note":
                result[
                    "reason"
                ],
        })

    # -----------------------------------------------------
    # WRITE CSV
    # -----------------------------------------------------

    report_path = (
        OUTPUT_DIR
        / "b2b_pricing_comparison.csv"
    )

    fieldnames = [
        "sku",
        "product",
        "handle",
        "vendor",

        "retail_price",

        "current_b2b_price",
        "current_price_source",
        "current_fixed_price",
        "current_relative_price",

        "calculated_b2b_price",
        "difference",
        "action",

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

        writer.writerows(
            report_rows
        )

    # -----------------------------------------------------
    # SUMMARY
    # -----------------------------------------------------

    catalog = (
        price_list.get(
            "catalog"
        )
        or {}
    )

    summary = {

        "catalog_title":
            CATALOG_TITLE,

        "catalog_id":
            catalog.get(
                "id"
            ),

        "price_list_id":
            price_list.get(
                "id"
            ),

        "currency":
            price_list.get(
                "currency"
            ),

        "parent_adjustment":
            adjustment,

        **counts,

        "mode":
            "B2B_COMPARISON_DRY_RUN_READ_ONLY",

        "shopify_changes":
            0,

        "report":
            str(
                report_path
            ),
    }

    summary_path = (
        OUTPUT_DIR
        / "b2b_comparison_summary.json"
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
        "B2B COMPARISON COMPLETE"
    )

    print(
        "NO SHOPIFY DATA WAS CHANGED."
    )

    print()

    print(
        json.dumps(
            summary,
            indent=2,
        )
    )


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():

    print(
        "FAST LANE SPARES"
    )

    print(
        "B2B PRICING COMPARISON"
    )

    print(
        "READ ONLY - "
        "NO SHOPIFY WRITES"
    )

    print()

    print(
        f"Settings file: "
        f"{SETTINGS_PATH}"
    )

    print(
        f"Target catalog: "
        f"{CATALOG_TITLE}"
    )

    print(
        f"Normal freight: "
        f"${NORMAL_FREIGHT_PER_KG}/kg"
    )

    print(
        f"Holley Direct freight: "
        f"${HOLLEY_DIRECT_FREIGHT_PER_KG}/kg"
    )

    print()

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

    fixed_prices = (
        fetch_fixed_b2b_prices(
            store,
            token,
            price_list[
                "id"
            ],
        )
    )

    create_report(
        variants,
        fixed_prices,
        price_list,
    )


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
