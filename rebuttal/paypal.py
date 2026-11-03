"""The PayPal REST calls Rebuttal makes, against the sandbox (or live) API.

FakePayPal in fake_paypal.py has the same methods, so the app and the tests can run without
sandbox accounts. Dispute objects are kept as PayPal's own JSON throughout.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

SANDBOX = "https://api-m.sandbox.paypal.com"
LIVE = "https://api-m.paypal.com"


class PayPalError(Exception):
    def __init__(self, status: int, body: Any, path: str = ""):
        self.status, self.body, self.path = status, body, path
        name = body.get("name") if isinstance(body, dict) else ""
        msg = body.get("message") if isinstance(body, dict) else str(body)[:300]
        super().__init__(f"PayPal {status} on {path}: {name} {msg}".strip())


def link(dispute: dict, rel: str) -> dict | None:
    """The HATEOAS link PayPal attaches when an action is currently allowed."""
    return next((lk for lk in dispute.get("links", []) if lk.get("rel") == rel), None)


class PayPal:
    """A thin client: OAuth token caching plus one method per call Rebuttal needs."""

    def __init__(self, client_id: str, secret: str, base: str = SANDBOX, http: httpx.Client | None = None):
        self.client_id, self.secret, self.base = client_id, secret, base
        self.http = http or httpx.Client(base_url=base, timeout=60)
        self._token: str | None = None
        self._token_expiry = 0.0

    @property
    def is_sandbox(self) -> bool:
        return self.base == SANDBOX

    def token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        r = self.http.post("/v1/oauth2/token", data={"grant_type": "client_credentials"},
                           auth=(self.client_id, self.secret))
        if r.status_code != 200:
            raise PayPalError(r.status_code, _body(r), "/v1/oauth2/token")
        body = r.json()
        self._token, self._token_expiry = body["access_token"], time.time() + body.get("expires_in", 3000)
        return self._token

    def _req(self, method: str, path: str, **kw) -> dict:
        headers = {"Authorization": f"Bearer {self.token()}", **kw.pop("headers", {})}
        r = self.http.request(method, path, headers=headers, **kw)
        if r.status_code >= 400:
            raise PayPalError(r.status_code, _body(r), path)
        return r.json() if r.content else {}

    def _multipart(self, path: str, input_json: dict, files: list[tuple[str, bytes, str]] = ()) -> dict:
        parts = [("input", (None, json.dumps(input_json), "application/json"))]
        parts += [(name, (name, data, ctype)) for name, data, ctype in files]
        return self._req("POST", path, files=parts)

    # --- disputes -------------------------------------------------------------------------

    def list_disputes(self, states: str = "REQUIRED_ACTION,REQUIRED_OTHER_PARTY_ACTION,UNDER_PAYPAL_REVIEW"
                      ) -> list[dict]:
        """Summaries of the seller's disputes, following pagination."""
        out, params = [], {"page_size": 50, "dispute_state": states}
        path = "/v1/customer/disputes"
        while path:
            body = self._req("GET", path, params=params)
            out += body.get("items", [])
            nxt = next((lk["href"] for lk in body.get("links", []) if lk.get("rel") == "next"), None)
            path, params = (nxt.removeprefix(self.base), None) if nxt else (None, None)
        return out

    def get_dispute(self, dispute_id: str) -> dict:
        return self._req("GET", f"/v1/customer/disputes/{dispute_id}")

    def provide_evidence(self, dispute_id: str, evidences: list[dict],
                         files: list[tuple[str, bytes, str]] = ()) -> dict:
        """Evidence JSON plus documents; each document's name must match a file part's name."""
        return self._multipart(f"/v1/customer/disputes/{dispute_id}/provide-evidence",
                               {"evidences": evidences}, files)

    def provide_supporting_info(self, dispute_id: str, notes: str,
                                files: list[tuple[str, bytes, str]] = ()) -> dict:
        """For a claim PayPal is already reviewing: notes plus documents, no evidence types."""
        return self._multipart(f"/v1/customer/disputes/{dispute_id}/provide-supporting-info",
                               {"notes": notes}, files)

    def make_offer(self, dispute_id: str, offer_type: str, amount: dict | None, note: str) -> dict:
        body: dict[str, Any] = {"note": note, "offer_type": offer_type}
        if amount:
            body["offer_amount"] = amount
        return self._req("POST", f"/v1/customer/disputes/{dispute_id}/make-offer", json=body)

    def accept_claim(self, dispute_id: str, note: str, claim_type: str = "REFUND") -> dict:
        return self._multipart(f"/v1/customer/disputes/{dispute_id}/accept-claim",
                               {"note": note, "accept_claim_type": claim_type})

    def send_message(self, dispute_id: str, message: str) -> dict:
        return self._multipart(f"/v1/customer/disputes/{dispute_id}/send-message", {"message": message})

    # Sandbox-only: move a dispute through its life cycle for testing and the demo.

    def require_evidence(self, dispute_id: str, action: str = "SELLER_EVIDENCE") -> dict:
        return self._req("POST", f"/v1/customer/disputes/{dispute_id}/require-evidence",
                         json={"action": action})

    def adjudicate(self, dispute_id: str, outcome: str) -> dict:
        return self._req("POST", f"/v1/customer/disputes/{dispute_id}/adjudicate",
                         json={"adjudication_outcome": outcome})

    # --- orders and tracking (the demo storefront) ----------------------------------------

    def create_order(self, amount: str, currency: str, description: str, reference_id: str) -> dict:
        return self._req("POST", "/v2/checkout/orders", json={
            "intent": "CAPTURE",
            "purchase_units": [{"reference_id": reference_id, "description": description,
                                "amount": {"currency_code": currency, "value": amount}}],
        })

    def capture_order(self, order_id: str) -> dict:
        return self._req("POST", f"/v2/checkout/orders/{order_id}/capture",
                         headers={"Content-Type": "application/json"})

    def add_tracker(self, transaction_id: str, tracking_number: str, carrier: str,
                    status: str = "SHIPPED") -> dict:
        return self._req("POST", "/v1/shipping/trackers-batch", json={"trackers": [{
            "transaction_id": transaction_id, "tracking_number": tracking_number,
            "status": status, "carrier": carrier}]})

    # --- webhooks ---------------------------------------------------------------------------

    def verify_webhook(self, headers: dict, event: dict, webhook_id: str) -> bool:
        body = self._req("POST", "/v1/notifications/verify-webhook-signature", json={
            "auth_algo": headers.get("paypal-auth-algo"),
            "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        })
        return body.get("verification_status") == "SUCCESS"


def _body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text
