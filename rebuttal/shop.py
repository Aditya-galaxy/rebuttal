"""The seller's own records: orders, shipments with carrier events, buyer messages, listings, policies.

In production this is a connector to the seller's store (Shopify, WooCommerce, a spreadsheet).
For the demo it is a SQLite file seeded with Ember & Clay, a small ceramics studio in Jaipur that
sells to buyers in the US through PayPal. The storefront adds real sandbox orders to the same tables.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
create table if not exists products (
    sku text primary key, title text, description text, price text, listing_url text, photos text);
create table if not exists orders (
    id text primary key, paypal_order_id text, transaction_id text unique, created_at text,
    buyer_name text, buyer_email text, ship_to text, items text, total text, currency text);
create table if not exists shipments (
    order_id text primary key, carrier text, tracking_number text, service text,
    status text, signature text, events text);
create table if not exists messages (
    id integer primary key autoincrement, order_id text, at text, sender text, channel text, body text);
create table if not exists policies (kind text primary key, title text, body text, url text);
"""


class Shop:
    def __init__(self, path: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- reads used by the agent's tools ------------------------------------------------------

    def order_for_transaction(self, transaction_id: str) -> dict | None:
        row = self.db.execute("select * from orders where transaction_id = ?", (transaction_id,)).fetchone()
        return _order(row) if row else None

    def order(self, order_id: str) -> dict | None:
        row = self.db.execute("select * from orders where id = ?", (order_id,)).fetchone()
        return _order(row) if row else None

    def shipment(self, order_id: str) -> dict | None:
        row = self.db.execute("select * from shipments where order_id = ?", (order_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["events"] = json.loads(d["events"] or "[]")
        return d

    def messages(self, order_id: str) -> list[dict]:
        rows = self.db.execute("select * from messages where order_id = ? order by at", (order_id,))
        return [dict(r) for r in rows]

    def product(self, sku: str) -> dict | None:
        row = self.db.execute("select * from products where sku = ?", (sku,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["photos"] = json.loads(d["photos"] or "[]")
        return d

    def products(self) -> list[dict]:
        return [self.product(r["sku"]) for r in self.db.execute("select sku from products order by sku")]

    def policy(self, kind: str) -> dict | None:
        row = self.db.execute("select * from policies where kind = ?", (kind,)).fetchone()
        return dict(row) if row else None

    def policies(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("select * from policies order by kind")]

    def record_exists(self, ref: str) -> bool:
        """Whether a citation like 'order:EC-2041' or 'message:12' points at a real record."""
        kind, _, key = ref.partition(":")
        table = {"order": ("orders", "id"), "shipment": ("shipments", "order_id"),
                 "message": ("messages", "id"), "product": ("products", "sku"),
                 "policy": ("policies", "kind")}.get(kind)
        if not table or not key:
            return False
        return self.db.execute(f"select 1 from {table[0]} where {table[1]} = ?", (key,)).fetchone() is not None

    # --- writes (storefront and seeding) ------------------------------------------------------

    def add_product(self, sku, title, description, price, listing_url, photos=()):
        self.db.execute("insert or replace into products values (?,?,?,?,?,?)",
                        (sku, title, description, price, listing_url, json.dumps(list(photos))))
        self.db.commit()

    def add_order(self, id, transaction_id, buyer_name, buyer_email, ship_to, items, total,
                  currency="USD", created_at=None, paypal_order_id=None):
        self.db.execute("insert or replace into orders values (?,?,?,?,?,?,?,?,?,?)",
                        (id, paypal_order_id, transaction_id, created_at or _now(), buyer_name, buyer_email,
                         ship_to, json.dumps(items), total, currency))
        self.db.commit()

    def add_shipment(self, order_id, carrier, tracking_number, service, status, signature, events):
        self.db.execute("insert or replace into shipments values (?,?,?,?,?,?,?)",
                        (order_id, carrier, tracking_number, service, status, signature, json.dumps(events)))
        self.db.commit()

    def add_message(self, order_id, at, sender, channel, body):
        self.db.execute("insert into messages (order_id, at, sender, channel, body) values (?,?,?,?,?)",
                        (order_id, at, sender, channel, body))
        self.db.commit()

    def set_policy(self, kind, title, body, url):
        self.db.execute("insert or replace into policies values (?,?,?,?)", (kind, title, body, url))
        self.db.commit()

    def next_order_id(self) -> str:
        row = self.db.execute("select max(cast(substr(id, 4) as integer)) from orders").fetchone()
        return f"EC-{(row[0] or 2000) + 1}"

    def is_empty(self) -> bool:
        return self.db.execute("select count(*) from products").fetchone()[0] == 0


def _order(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["items"] = json.loads(d["items"] or "[]")
    return d


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ago(days: float, base: datetime) -> str:
    return (base - timedelta(days=days)).isoformat(timespec="seconds")


SITE = "https://emberandclay.example"


def seed_demo(shop: Shop, now: datetime | None = None) -> None:
    """Ember & Clay: four orders that each became a different kind of dispute."""
    now = now or datetime.now(UTC)
    a = lambda d: _ago(d, now)  # noqa: E731

    shop.add_product("VASE-INDIGO", "Indigo Moon Vase",
                     "Wheel-thrown stoneware vase, 32 cm, indigo crackle glaze. Ships in a double box.",
                     "148.00", f"{SITE}/p/indigo-moon-vase", ["vase-front.jpg", "vase-packed.jpg"])
    shop.add_product("CHAI-RUST", "Chai Cups, set of 4 (Rust glaze)",
                     "Four 150 ml cups. Each glaze is fired by hand, so colour varies from cup to cup and "
                     "from the photos; no two sets match.", "62.00", f"{SITE}/p/chai-cups-rust",
                     ["cups-set.jpg", "cups-glaze-range.jpg"])
    shop.add_product("COASTER-BLOCK", "Block-print coasters, set of 6",
                     "Hand-painted terracotta coasters with cork backs.", "35.00",
                     f"{SITE}/p/block-print-coasters", ["coasters.jpg"])
    shop.add_product("PLANTER-PAIR", "Terracotta planter pair",
                     "Two unglazed terracotta planters, 18 cm and 24 cm, with saucers.", "210.00",
                     f"{SITE}/p/terracotta-planters", ["planters.jpg"])

    shop.set_policy("returns", "Returns and refunds",
                    "Returns accepted within 30 days of delivery for items in original condition. "
                    "Buyer pays return shipping unless the item arrived damaged. Refund issued within "
                    "5 days of the return arriving in Jaipur. Colour variation in hand-fired glazes is "
                    "not a defect and is disclosed on every listing.", f"{SITE}/policies/returns")
    shop.set_policy("shipping", "Shipping",
                    "Orders ship from Jaipur within 3 working days. Orders over $100 ship by DHL Express "
                    "or FedEx International Priority with signature on delivery. Smaller orders ship by "
                    "India Post registered airmail (10-20 working days).", f"{SITE}/policies/shipping")

    # 1. Not received, but DHL has a signed delivery: fight with proof of fulfilment.
    shop.add_order("EC-2041", "7KD04291VX3301845", "Jordan Miller", "jordan.miller@example.com",
                   "Jordan Miller, 4417 SE Belmont St, Portland, OR 97215, US",
                   [{"sku": "VASE-INDIGO", "title": "Indigo Moon Vase", "qty": 1, "price": "148.00"}],
                   "148.00", created_at=a(22))
    shop.add_shipment("EC-2041", "DHL", "4589203716", "DHL Express Worldwide", "DELIVERED", "J. MILLER", [
        {"at": a(21), "where": "Jaipur, IN", "event": "Shipment picked up"},
        {"at": a(20.6), "where": "New Delhi, IN", "event": "Departed facility"},
        {"at": a(19.8), "where": "Cincinnati Hub, OH, US", "event": "Arrived at facility"},
        {"at": a(19.5), "where": "Cincinnati Hub, OH, US", "event": "Customs clearance completed"},
        {"at": a(18.2), "where": "Portland, OR, US", "event": "With delivery courier"},
        {"at": a(18.0), "where": "Portland, OR, US", "event": "Delivered, signed for by J. MILLER"},
    ])
    shop.add_message("EC-2041", a(21), "seller", "email",
                     "Hi Jordan, your Indigo Moon Vase shipped today by DHL Express, tracking 4589203716. "
                     "It needs a signature on delivery.")

    # 2. Not as described (glaze colour); variation is disclosed. Return shipping to India costs more
    #    than the cups, so a partial refund that lets the buyer keep them is the cheapest good outcome.
    shop.add_order("EC-2055", "2MW71520LQ8846217", "Priya Raman", "priya.raman@example.com",
                   "Priya Raman, 88 Orchard Ln, Edison, NJ 08817, US",
                   [{"sku": "CHAI-RUST", "title": "Chai Cups, set of 4 (Rust glaze)", "qty": 1,
                     "price": "62.00"}], "62.00", created_at=a(19))
    shop.add_shipment("EC-2055", "FedEx", "771893450012", "FedEx International Economy", "DELIVERED", None, [
        {"at": a(18), "where": "Jaipur, IN", "event": "Picked up"},
        {"at": a(13), "where": "Newark, NJ, US", "event": "International shipment release"},
        {"at": a(11), "where": "Edison, NJ, US", "event": "Delivered, left at front door"},
    ])
    shop.add_message("EC-2055", a(10), "buyer", "email",
                     "The cups arrived safely, thank you. They are a lot darker than the photos though, "
                     "more brown than rust. Is that normal?")
    shop.add_message("EC-2055", a(9.5), "seller", "email",
                     "Hi Priya, thank you for writing. Each glaze is fired by hand so the colour shifts "
                     "from kiln to kiln, as the listing mentions. If you'd like, I can offer a partial "
                     "refund, or you can return them within 30 days.")

    # 3. Not received, and India Post tracking stopped 24 days ago: Rebuttal should say refund.
    shop.add_order("EC-2063", "5RT38812GH1190274", "Sam Okafor", "sam.okafor@example.com",
                   "Sam Okafor, 210 W 15th St Apt 4C, New York, NY 10011, US",
                   [{"sku": "COASTER-BLOCK", "title": "Block-print coasters, set of 6", "qty": 1,
                     "price": "35.00"}], "35.00", created_at=a(28))
    shop.add_shipment("EC-2063", "India Post", "RR482910375IN", "Registered airmail", "IN_TRANSIT", None, [
        {"at": a(27), "where": "Jaipur GPO, IN", "event": "Item booked"},
        {"at": a(25), "where": "Mumbai Foreign Post Office, IN", "event": "Item dispatched to destination country"},
    ])
    shop.add_message("EC-2063", a(6), "buyer", "email",
                     "It's been almost 4 weeks and tracking hasn't moved since it left Mumbai. Where is my order?")

    # 4. Unauthorised: the "stranger" wrote to the seller about gift wrap for this same order,
    #    from the account email, and FedEx delivered to the address on the PayPal transaction.
    shop.add_order("EC-2070", "9HB55107ZC2238461", "Alex Chen", "alex.chen@example.com",
                   "Alex Chen, 1550 Mission Ridge Dr, Austin, TX 78704, US",
                   [{"sku": "PLANTER-PAIR", "title": "Terracotta planter pair", "qty": 1, "price": "210.00"}],
                   "210.00", created_at=a(16))
    shop.add_message("EC-2070", a(15.8), "buyer", "email",
                     "Hi! Just ordered the planter pair. Could you gift wrap them? They're a housewarming "
                     "present for my sister. Thanks, Alex")
    shop.add_message("EC-2070", a(15.6), "seller", "email",
                     "Of course, Alex. I'll wrap both in kraft paper with a card. Ships by Friday.")
    shop.add_shipment("EC-2070", "FedEx", "771894120587", "FedEx International Priority", "DELIVERED",
                      "A. CHEN", [
                          {"at": a(14), "where": "Jaipur, IN", "event": "Picked up"},
                          {"at": a(11), "where": "Memphis, TN, US", "event": "International shipment release"},
                          {"at": a(10), "where": "Austin, TX, US", "event": "Delivered, signed for by A. CHEN"},
                      ])
