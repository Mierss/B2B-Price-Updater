# Fast Lane Spares B2B Pricing Automation

Automates B2B pricing for the Shopify native catalog:

`Wholesale 10%`

The current version is **read-only / dry-run only**.

## What it does

The script reads Shopify product data including:

* SKU
* Retail price
* Cost
* Weight
* Vendor
* Tags

It then:

1. Calculates the correct B2B price
2. Reads the current Shopify B2B catalog price
3. Compares current vs calculated pricing
4. Produces an audit report
5. Makes no Shopify changes

## Pricing Rules

Pricing settings are stored in:

`b2b_settings.json`

This is the main file to edit when changing:

* Brand discounts
* Margin floors
* Freight rates
* Price thresholds
* Catalog name

Example:

```json
"aeroflow": 0.20,
"proflow": 0.20,
"turbosmart": 0.05
```

## Freight

If Shopify cost exists:

```text
Holley Direct tag = $10/kg
Everything else = $8/kg
```

Landed cost:

```text
Cost + Freight
```

If cost is missing, the script skips the margin-floor calculation and uses the applicable discount only.

## Margin Protection

Current rules:

```text
Landed cost up to $1,500 = minimum 25% margin
Landed cost over $1,500 = minimum 20% margin
```

Final B2B price:

```text
Higher of:
Discount Price
or
Margin Floor Price

But never higher than retail price.
```

## Main Files

`run_b2b_dry_run.py`

Main pricing and Shopify comparison script.

`b2b_settings.json`

Editable pricing rules.

`requirements.txt`

Python dependencies.

`.github/workflows/b2b-pricing-dry-run.yml`

GitHub Action used to run the dry run.

## Running It

In GitHub:

```text
Actions
→ B2B Pricing DRY RUN
→ Run workflow
```

The output includes:

`b2b_pricing_comparison.csv`

`b2b_comparison_summary.json`

The report shows:

```text
NO_CHANGE
WOULD_UPDATE
WOULD_ADD_FIXED_PRICE
```

## Safety

The current script is read-only.

It does not change:

* Shopify retail pricing
* Cost
* Weight
* Products
* Inventory
* B2B catalog pricing

Once the dry-run results are fully verified, a separate live updater can be added to automatically update only changed B2B prices.
