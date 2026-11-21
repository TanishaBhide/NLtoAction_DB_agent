"""Generate a larger, realistic Online Retail-style dataset (deterministic, seeded).

The original UCI Online Retail CSV is not shipped with the repository, so this
script synthesises data with the same shape and business rules, ON TOP OF the
13 real sample rows in sample_data.sql (which the documented demos rely on):

    customers      ~120   5-digit ids, ~70% United Kingdom, the rest across 20 countries
    products       ~200   Online Retail-style stock codes and upper-case descriptions,
                          plus the non-merchandise codes POST / DOT / M
    invoices       ~370   Dec 2010 - Dec 2011, shop hours; ~8% guest (no customer)
                          ~4% cancellations: 'C' prefix, negative quantities copied from
                          an earlier order of the same customer; 2 'A' bad-debt adjustments
    invoice_items  2,000 in total (sample rows included)

Every rule the schema and the Week 6 invariants enforce holds: quantity <> 0,
negative quantities only on cancelled invoices, negative prices only on
adjustment invoices, status matches the invoice prefix, every invoice has at
least one line item.

    python database/generate_retail_data.py            # writes database/sample_data_large.sql
    python database/generate_retail_data.py --items 5000
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).with_name("sample_data_large.sql")
SEED = 20101201

# rows already in sample_data.sql (kept unique)
SAMPLE_CUSTOMERS = {"17850", "12494", "14527"}
SAMPLE_PRODUCTS = {"85123A", "71053", "84406B", "84029G", "84029E", "22752", "21730", "21843", "22667",
                   "POST", "22139", "D", "B"}
SAMPLE_INVOICES = {"536365", "537967", "536414", "C536379", "A563186"}
SAMPLE_ITEMS = 13

COUNTRIES = [("United Kingdom", 70), ("Germany", 5), ("France", 5), ("EIRE", 3), ("Spain", 2), ("Netherlands", 2),
             ("Belgium", 2), ("Switzerland", 1.5), ("Portugal", 1.5), ("Australia", 1), ("Norway", 1),
             ("Italy", 1), ("Channel Islands", 0.8), ("Finland", 0.8), ("Cyprus", 0.6), ("Sweden", 0.6),
             ("Austria", 0.5), ("Denmark", 0.5), ("Japan", 0.5), ("Poland", 0.4), ("USA", 0.4)]

COLOURS = ["WHITE", "RED", "PINK", "BLUE", "GREEN", "IVORY", "BLACK", "CREAM", "VINTAGE", "RETROSPOT",
           "SPOTTY", "STRIPEY", "GINGHAM", "PAISLEY", "SKULLS", "DOLLY GIRL", "SPACEBOY", "REGENCY", "JUMBO",
           "SET OF 3", "SET OF 6", "PACK OF 12", "WOODLAND", "ROSES", "HEART", "STAR", "BIRD", "GLASS",
           "ZINC", "ENAMEL", "FELTCRAFT", "LOVE", "CHRISTMAS", "PARTY", "HANGING"]
ITEMS = [("T-LIGHT HOLDER", 1.25, 4.25), ("LUNCH BAG", 1.65, 2.10), ("CAKE STAND", 4.95, 10.95),
         ("PAPER CHAIN KIT", 2.55, 2.95), ("HOT WATER BOTTLE", 3.39, 4.95), ("ALARM CLOCK BAKELIKE", 3.75, 4.25),
         ("NAPKINS", 0.85, 0.85), ("JUMBO BAG", 1.65, 2.08), ("CAKE CASES", 0.42, 0.55), ("TEA CUP AND SAUCER", 2.95, 3.25),
         ("CHILDRENS APRON", 1.65, 1.95), ("DOORMAT", 6.75, 7.95), ("PHOTO FRAME", 1.25, 2.95),
         ("LANTERN", 3.75, 4.95), ("BUNTING", 2.95, 4.95), ("MUG", 1.25, 1.65), ("TIN", 1.65, 2.95),
         ("BOTTLE", 1.25, 2.10), ("WALL ART", 4.95, 6.75), ("CUSHION COVER", 3.75, 4.95), ("PLASTERS IN TIN", 1.65, 1.65),
         ("STORAGE JAR", 2.95, 3.75), ("BAKING SET", 4.95, 4.95), ("CANDLE", 0.29, 1.25), ("COAT HANGER", 1.65, 3.25),
         ("HAND WARMER", 2.10, 2.10), ("NESTING BOXES", 7.65, 8.50), ("RECIPE BOX", 2.95, 2.95),
         ("NIGHT LIGHT", 1.65, 2.10), ("DRAWER KNOB", 1.25, 1.65), ("PICNIC BASKET", 2.95, 4.95), ("GIFT WRAP", 0.42, 0.42),
         ("CARD HOLDER", 2.10, 3.75), ("BELL DECORATION", 0.39, 0.85), ("SHOPPING BAG", 1.65, 2.10)]


def weighted(rng: random.Random, pairs: list[tuple[str, float]]) -> str:
    return rng.choices([p for p, _ in pairs], weights=[w for _, w in pairs])[0]


def q(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return f"{value:.2f}" if isinstance(value, float) else str(value)
    return "'" + str(value).replace("'", "''") + "'"


def generate(total_items: int = 2000, seed: int = SEED) -> tuple[str, dict[str, int]]:
    rng = random.Random(seed)

    # ---------------------------------------------------------- customers
    customers: dict[str, str] = {}
    while len(customers) < 120:
        cid = str(rng.randint(12346, 18287))
        if cid not in SAMPLE_CUSTOMERS:
            customers[cid] = weighted(rng, COUNTRIES)
    customer_ids = list(customers)
    loyalty = {cid: min(rng.lognormvariate(0, 0.8), 6.0) for cid in customer_ids}  # some loyal customers

    # ----------------------------------------------------------- products
    products: dict[str, tuple[str, float]] = {}
    used_names: set[str] = set()
    while len(products) < 200:
        colour, (item, low, high) = rng.choice(COLOURS), rng.choice(ITEMS)
        name = f"{colour} {item}"
        if name in used_names:
            continue
        code = str(rng.randint(20600, 23500)) if rng.random() < 0.75 else f"{rng.randint(82000, 90210)}{rng.choice('ABCDEFG')}"
        if code in SAMPLE_PRODUCTS or code in products:
            continue
        used_names.add(name)
        products[code] = (name, round(rng.choice([low, high, (low + high) / 2]), 2))
    products["DOT"] = ("DOTCOM POSTAGE", 0.0)
    products["M"] = ("Manual", 0.0)
    merchandise = [c for c in products if c not in {"DOT", "M"}]
    popularity = {c: min(rng.lognormvariate(0, 0.9), 8.0) for c in merchandise}  # best-sellers exist

    # ----------------------------------------------------------- invoices
    start, end = datetime(2010, 12, 1, 8, 0), datetime(2011, 12, 9, 18, 0)
    span_days = (end - start).days
    invoice_rows: list[tuple[str, datetime, str | None]] = []
    item_rows: list[tuple[str, str, int, float]] = []
    orders_by_customer: dict[str, list[list[tuple[str, int, float]]]] = {}
    used_numbers = {int(n) for n in SAMPLE_INVOICES if n.isdigit()}
    budget = total_items - SAMPLE_ITEMS

    def new_number(prefix: str = "") -> str:
        while True:
            n = rng.randint(536370, 581580)
            if n not in used_numbers:
                used_numbers.add(n)
                return f"{prefix}{n}"

    def when() -> datetime:
        day = start + timedelta(days=rng.randint(0, span_days))
        if day.weekday() == 5:  # the shop did not trade on Saturdays
            day += timedelta(days=1)
        return day.replace(hour=rng.randint(8, 17), minute=rng.randint(0, 59))

    # two bad-debt adjustments (negative price on an 'A' invoice)
    for _ in range(2):
        number = new_number("A")
        invoice_rows.append((number, when(), None))
        item_rows.append((number, "B", 1, -round(rng.uniform(200, 4000), 2)))
        budget -= 1

    while budget > 0:
        guest = rng.random() < 0.08
        customer = None if guest else rng.choices(customer_ids, weights=[loyalty[c] for c in customer_ids])[0]
        lines = min(budget, max(1, int(rng.lognormvariate(1.5, 0.7))))
        if lines > 30:
            lines = 30
        number, stamp = new_number(), when()
        chosen = set()
        order: list[tuple[str, int, float]] = []
        for _ in range(lines):
            code = rng.choices(merchandise, weights=[popularity[c] for c in merchandise])[0]
            if code in chosen:
                continue
            chosen.add(code)
            qty = rng.choice([1, 1, 2, 2, 3, 4, 6, 6, 8, 10, 12, 12, 12, 24, 24, 36, 48])
            if rng.random() < 0.02:
                qty = rng.choice([72, 96, 120])
            price = products[code][1]
            if qty >= 24 and rng.random() < 0.6:  # wholesale discount
                price = round(price * 0.85, 2)
            order.append((code, qty, max(price, 0.19)))
        if rng.random() < 0.12:
            order.append(("POST", 1, rng.choice([15.0, 18.0, 28.0, 40.0])))
        order = order[:budget]
        invoice_rows.append((number, stamp, customer))
        item_rows += [(number, code, qty, price) for code, qty, price in order]
        budget -= len(order)
        if customer:
            orders_by_customer.setdefault(customer, []).append(order)
        # ~4% of customer orders are later (partly) cancelled
        if customer and budget > 0 and rng.random() < 0.045:
            cancel_lines = rng.sample(order, k=min(len(order), rng.randint(1, 3), budget))
            cancel_no = new_number("C")
            invoice_rows.append((cancel_no, stamp + timedelta(hours=rng.randint(2, 200)), customer))
            item_rows += [(cancel_no, code, -qty, price) for code, qty, price in cancel_lines]
            budget -= len(cancel_lines)

    # ------------------------------------------------------------ SQL file
    stats = {"customers": len(customers), "products": len(products), "invoices": len(invoice_rows),
             "invoice_items": len(item_rows), "total_invoice_items": len(item_rows) + SAMPLE_ITEMS}
    lines_sql = [
        "-- Generated by database/generate_retail_data.py (seed %d); do not edit by hand." % seed,
        "-- Adds a larger Online Retail-style dataset ON TOP OF sample_data.sql.",
        f"-- customers +{stats['customers']}, products +{stats['products']}, invoices +{stats['invoices']}, "
        f"invoice_items +{stats['invoice_items']} (total line items {stats['total_invoice_items']}).",
        "",
        "INSERT INTO customers (customer_id, country) VALUES",
        ",\n".join(f"    ({q(c)}, {q(country)})" for c, country in sorted(customers.items())) + ";",
        "",
        "INSERT INTO products (stock_code, description) VALUES",
        ",\n".join(f"    ({q(c)}, {q(name)})" for c, (name, _) in sorted(products.items())) + ";",
        "",
        "-- status is set by trg_invoices_set_status from the invoice-number prefix",
        "INSERT INTO invoices (invoice_no, invoice_date, customer_id) VALUES",
        ",\n".join(f"    ({q(n)}, '{d:%Y-%m-%d %H:%M}', {q(c)})"
                   for n, d, c in sorted(invoice_rows, key=lambda r: (r[1], r[0]))) + ";",
        "",
        "INSERT INTO invoice_items (invoice_no, stock_code, quantity, unit_price) VALUES",
        ",\n".join(f"    ({q(n)}, {q(c)}, {qty}, {price:.2f})" for n, c, qty, price in item_rows) + ";",
        "",
    ]
    return "\n".join(lines_sql), stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--items", type=int, default=2000, help="Total line items including the 13 sample rows")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    sql, stats = generate(args.items, args.seed)
    args.out.write_text(sql, encoding="utf-8")
    print(f"wrote {args.out} : " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
