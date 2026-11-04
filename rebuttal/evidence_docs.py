"""Evidence documents as PDFs, built only from the seller's records.

Every page footer lists the records it was built from, so a reviewer at PayPal (or a judge) can
see nothing was invented. PayPal accepts PDF, JPG, PNG and GIF; we send PDFs.
"""

from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

from .playbook import parse_time

IMAGES = Path(__file__).parent / "static" / "img"

INK = (28, 31, 38)
MUTED = (110, 116, 128)
RULE = (220, 223, 230)


class _Doc(FPDF):
    def __init__(self, title: str, shop_name: str, sources: list[str]):
        super().__init__(format="A4")
        self.doc_title, self.shop_name, self.sources = title, shop_name, sources
        self.set_auto_page_break(True, margin=22)
        self.set_margins(18, 18, 18)
        self.add_page()

    def header(self):
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*MUTED)
        self.cell(0, 5, _safe(self.shop_name.upper()), new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "B", 17)
        self.set_text_color(*INK)
        self.cell(0, 10, _safe(self.doc_title), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*RULE)
        self.line(18, self.get_y() + 1, 192, self.get_y() + 1)
        self.ln(5)

    def footer(self):
        self.set_y(-16)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*MUTED)
        self.multi_cell(0, 3.6, _safe("Built from the seller's records: " + ", ".join(self.sources)
                                      + f".   Page {self.page_no()}"))

    def kv(self, rows: list[tuple[str, str]]):
        for k, v in rows:
            self.set_font("Helvetica", "B", 9.5)
            self.set_text_color(*MUTED)
            self.cell(42, 6, _safe(k))
            self.set_font("Helvetica", "", 10)
            self.set_text_color(*INK)
            self.multi_cell(0, 6, _safe(v), new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

    def h2(self, text: str):
        self.ln(2)
        self.set_font("Helvetica", "B", 11.5)
        self.set_text_color(*INK)
        self.cell(0, 8, _safe(text), new_x="LMARGIN", new_y="NEXT")

    def para(self, text: str, size: float = 10):
        self.set_font("Helvetica", "", size)
        self.set_text_color(*INK)
        self.multi_cell(0, 5.4, _safe(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(1.5)

    def table(self, head: list[str], rows: list[list[str]], widths: list[float]):
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*MUTED)
        for h, w in zip(head, widths, strict=True):
            self.cell(w, 7, _safe(h), border="B")
        self.ln()
        self.set_font("Helvetica", "", 9.5)
        self.set_text_color(*INK)
        for row in rows:
            y0 = self.get_y()
            x = self.l_margin
            heights = []
            for cell, w in zip(row, widths, strict=True):
                self.set_xy(x, y0)
                self.multi_cell(w, 5.6, _safe(cell))
                heights.append(self.get_y() - y0)
                x += w
            self.set_y(y0 + max(heights) + 1)


def receipt(shop_name: str, order: dict, dispute_id: str) -> bytes:
    pdf = _Doc("Order receipt", shop_name, [f"order:{order['id']}"])
    pdf.kv([("Order", order["id"]), ("PayPal transaction", order["transaction_id"]),
            ("Placed", _when(order["created_at"])), ("Buyer", f"{order['buyer_name']} <{order['buyer_email']}>"),
            ("Ship to", order["ship_to"]), ("Dispute", dispute_id)])
    pdf.table(["Item", "SKU", "Qty", "Price"],
              [[i["title"], i["sku"], str(i["qty"]), f"${i['price']}"] for i in order["items"]], [92, 40, 16, 26])
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10.5)
    pdf.cell(0, 7, _safe(f"Total paid: ${order['total']} {order['currency']}"))
    return bytes(pdf.output())


def delivery(shop_name: str, order: dict, ship: dict) -> bytes:
    pdf = _Doc("Proof of delivery", shop_name, [f"order:{order['id']}", f"shipment:{order['id']}"])
    last = ship["events"][-1] if ship["events"] else {}
    pdf.kv([("Carrier", f"{ship['carrier']} ({ship['service']})"), ("Tracking number", ship["tracking_number"]),
            ("Status", ship["status"].replace("_", " ").title()),
            ("Signed for by", ship.get("signature") or "No signature recorded"),
            ("Delivered to", order["ship_to"] if ship["status"] == "DELIVERED" else "Not delivered"),
            ("Last event", f"{_when(last.get('at', ''))}, {last.get('where', '')}" if last else "None"),
            ("PayPal transaction", order["transaction_id"])])
    pdf.h2("Carrier event history")
    pdf.table(["When (UTC)", "Where", "Event"],
              [[_when(e["at"]), e["where"], e["event"]] for e in ship["events"]], [42, 52, 80])
    return bytes(pdf.output())


def messages(shop_name: str, order: dict, msgs: list[dict]) -> bytes:
    pdf = _Doc("Messages with the buyer", shop_name,
               [f"order:{order['id']}"] + [f"message:{m['id']}" for m in msgs])
    pdf.kv([("Order", order["id"]), ("Buyer", f"{order['buyer_name']} <{order['buyer_email']}>")])
    for m in msgs:
        who = order["buyer_name"] if m["sender"] == "buyer" else shop_name
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 6, _safe(f"{who}  ·  {m['channel']}  ·  {_when(m['at'])}"), new_x="LMARGIN", new_y="NEXT")
        pdf.para(m["body"])
    return bytes(pdf.output())


def listing(shop_name: str, product: dict, policy: dict | None, photo: dict | None = None) -> bytes:
    sources = [f"product:{product['sku']}"] + ([f"photo:{photo['id']}"] if photo else []) + (
        ["policy:returns"] if policy else [])
    pdf = _Doc("Listing and return policy", shop_name, sources)
    pdf.kv([("Item", product["title"]), ("Listing", product["listing_url"]), ("Price", f"${product['price']}")])
    if photo:
        pdf.image(str(IMAGES / photo["file"]), w=70)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 6, _safe(f"Listing photo, as shown to buyers ({photo['caption']})"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    pdf.h2("Description shown to the buyer")
    pdf.para(product["description"])
    if policy:
        pdf.h2(policy["title"])
        pdf.para(policy["body"])
        pdf.para(f"Published at {policy['url']}", size=8.5)
    return bytes(pdf.output())


def photo_comparison(shop_name: str, order: dict, listing_photo: dict, buyer_photo: dict,
                     check: dict | None) -> bytes:
    sources = [f"order:{order['id']}", f"photo:{listing_photo['id']}", f"photo:{buyer_photo['id']}"]
    pdf = _Doc("Listing photo and the buyer's photo", shop_name, sources)
    pdf.kv([("Order", order["id"]), ("Buyer's photo", f"Sent {_when(buyer_photo['at'])}")])
    y = pdf.get_y()
    pdf.image(str(IMAGES / listing_photo["file"]), x=18, y=y, w=84)
    pdf.image(str(IMAGES / buyer_photo["file"]), x=108, y=y, w=84)
    pdf.set_y(y + 86)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*MUTED)
    pdf.cell(90, 6, "Listing photo (what the buyer saw)")
    pdf.cell(0, 6, "Photo the buyer sent", new_x="LMARGIN", new_y="NEXT")
    if check:
        pdf.h2("What the photos show")
        pdf.para(check["observation"])
        pdf.para(f"Differences: {check['differences']}")
        pdf.para(f"Visible damage: {'yes' if check['damage_visible'] else 'none'}. "
                 f"Variation disclosed in the listing covers the difference: "
                 f"{'yes' if check['disclosed_variation_explains_it'] else 'no'}.")
        pdf.para(f"Observation by an AI model ({check.get('model', 'Gemini')}, Google Vertex AI) from the two "
                 "photos, reviewed by the seller before sending.", size=8.5)
    return bytes(pdf.output())


def _when(iso: str) -> str:
    return parse_time(iso).strftime("%d %b %Y %H:%M") if iso else ""


def _safe(s: str) -> str:
    """The core PDF fonts are Latin-1; swap the few characters our records use outside it."""
    return (str(s).replace("—", "-").replace("–", "-").replace("’", "'").replace("“", '"')
            .replace("”", '"').replace("·", "-").encode("latin-1", "replace").decode("latin-1"))
