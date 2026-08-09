#!/usr/bin/env python3
"""
Fast Lane Spares - B2B Pricing LIVE

- Uses the exact same pricing engine/settings as run_b2b_dry_run.py.
- Reads current Shopify B2B pricing.
- Updates ONLY prices that differ from the calculated B2B price.
- Adds missing fixed prices where required.
- Processes Shopify writes in batches of 250.
- Writes a full audit CSV and summary JSON.
"""

from __future__ import annotations

import csv
import json
import sys
from decimal import Decimal as D

from run_b2b_dry_run import (
    CATALOG_TITLE,
    OUTPUT_DIR,
    PRICE_TOLERANCE,
    calculate_b2b_price,
    calculate_relative_b2b_price,
    fetch_fixed_b2b_prices,
    fetch_shopify_variants,
    find_b2b_price_list,
    get_shopify_token,
    graphql,
    money,
    required_env,
)


BATCH_SIZE = 250


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


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
            "variantId": row["variant_id"],
            "price": {
                "amount": str(row["new_b2b_price"]),
                "currencyCode": currency,
            },
        })

    payload = graphql(
        store,
        token,
        mutation,
        {
            "priceListId": price_list_id,
            "prices": prices,
        },
    )

    result = payload[
        "data"
    ][
        "priceListFixedPricesAdd"
    ]

    errors = (
        result.get("userErrors")
        or []
    )

    if errors:
        raise RuntimeError(
            json.dumps(
                errors,
                indent=2,
            )
        )

    return (
        result.get("prices")
        or []
    )


def main():

    print("FAST LANE SPARES")
    print("B2B PRICING LIVE")
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

    token = get_shopify_token(
        store,
        client_id,
        client_secret,
    )

    # -----------------------------------------------------
    # READ SHOPIFY
    # -----------------------------------------------------

    variants = fetch_shopify_variants(
        store,
        token,
    )

    price_list = find_b2b_price_list(
        store,
        token,
    )

    price_list_id = (
        price_list["id"]
    )

    currency = (
        price_list.get("currency")
        or "NZD"
    )

    fixed_prices = (
        fetch_fixed_b2b_prices(
            store,
            token,
            price_list_id,
        )
    )

    parent = (
        price_list.get("parent")
        or {}
    )

    adjustment = (
        parent.get("adjustment")
        or {}
    )

    # -----------------------------------------------------
    # FIND ACTUAL CHANGES
    # -----------------------------------------------------

    candidates = []

    skipped = []

    no_change = 0

    for variant in variants:

        result = (
            calculate_b2b_price(
                variant
            )
        )

        if (
            result["status"]
            != "CALCULATED"
        ):
            skipped.append({
                "sku":
                    (
                        variant.get("sku")
                        or ""
                    ),
                "variant_id":
                    variant.get("id"),
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

        retail_price = (
            result[
                "retail_price"
            ]
        )

        calculated_price = (
            result[
                "b2b_price"
            ]
        )

        fixed_price = (
            fixed_prices.get(
                variant_id
            )
        )

        relative_price = (
            calculate_relative_b2b_price(
                retail_price,
                adjustment,
            )
        )

        if fixed_price is not None:

            current_price = (
                fixed_price
            )

            current_source = (
                "FIXED"
            )

        else:

            current_price = (
                relative_price
            )

            current_source = (
                "RELATIVE"
                if relative_price
                is not None
                else "NONE"
            )

        if (
            current_price is not None
            and abs(
                calculated_price
                - current_price
            )
            <= PRICE_TOLERANCE
        ):

            no_change += 1

            continue

        candidates.append({
            "sku":
                result["sku"],

            "product":
                result["product"],

            "vendor":
                result["vendor"],

            "variant_id":
                variant_id,

            "retail_price":
                retail_price,

            "old_b2b_price":
                current_price,

            "old_price_source":
                current_source,

            "new_b2b_price":
                calculated_price,

            "difference":
                (
                    calculated_price
                    - current_price
                    if current_price
                    is not None
                    else None
                ),

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

            "status":
                "PENDING",

            "error":
                "",
        })

    print()
    print(
        f"Target catalog: "
        f"{CATALOG_TITLE}"
    )

    print(
        f"Currency: {currency}"
    )

    print(
        f"Unchanged: "
        f"{no_change:,}"
    )

    print(
        f"Prices to write: "
        f"{len(candidates):,}"
    )

    print(
        f"Skipped: "
        f"{len(skipped):,}"
    )

    # -----------------------------------------------------
    # WRITE CHANGES
    # -----------------------------------------------------

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

            updated = (
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

            successful += len(
                batch
            )

            print(
                f"  Success: "
                f"{len(updated)} "
                f"prices returned "
                f"by Shopify."
            )

        except Exception as exc:

            failed += len(
                batch
            )

            for row in batch:
                row[
                    "status"
                ] = "ERROR"

                row[
                    "error"
                ] = str(exc)

            print(
                f"  ERROR: {exc}",
                file=sys.stderr,
            )

    # -----------------------------------------------------
    # AUDIT
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

            output_row = dict(row)

            output_row[
                "retail_price"
            ] = money(
                row[
                    "retail_price"
                ]
            )

            output_row[
                "old_b2b_price"
            ] = money(
                row[
                    "old_b2b_price"
                ]
            )

            output_row[
                "new_b2b_price"
            ] = money(
                row[
                    "new_b2b_price"
                ]
            )

            output_row[
                "difference"
            ] = money(
                row[
                    "difference"
                ]
            )

            output_row[
                "landed_cost"
            ] = money(
                row[
                    "landed_cost"
                ]
            )

            output_row[
                "margin_floor"
            ] = money(
                row[
                    "margin_floor"
                ]
            )

            writer.writerow(
                output_row
            )

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
            no_change,

        "attempted_updates":
            len(candidates),

        "successful":
            successful,

        "failed":
            failed,

        "skipped":
            len(skipped),

        "audit_file":
            str(audit_path),

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
