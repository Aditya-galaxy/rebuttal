"""Command line: `rebuttal check` tests the PayPal and model settings without printing any secret."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def load_env(path: str | Path = ".env") -> None:
    """Read KEY=value lines into the environment. Values already set in the shell win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def check() -> int:
    from .agent import LLM
    from .paypal import LIVE, SANDBOX, PayPal, PayPalError

    load_env()
    ok = True
    mode = os.environ.get("PAYPAL_MODE", "mock")
    print(f"PAYPAL_MODE = {mode}")
    if mode == "mock":
        print("  Mock mode: no PayPal calls. Set PAYPAL_MODE=sandbox in .env to use your sandbox app.")
    else:
        cid, secret = os.environ.get("PAYPAL_CLIENT_ID", ""), os.environ.get("PAYPAL_CLIENT_SECRET", "")
        if not cid or not secret:
            print("  ✕ PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET is empty in .env")
            return 1
        print(f"  client id {cid[:6]}…{cid[-4:]} ({len(cid)} chars), secret {len(secret)} chars")
        pp = PayPal(cid, secret, LIVE if mode == "live" else SANDBOX)
        try:
            pp.token()
            print("  ✓ PayPal accepted the credentials")
        except PayPalError as e:
            print(f"  ✕ PayPal refused the credentials: {e}")
            return 1
        try:
            items = pp.list_disputes()
            print(f"  ✓ Disputes API works: {len(items)} open dispute(s)")
        except PayPalError as e:
            ok = False
            print(f"  ✕ Disputes API refused: {e}")
            print("    In the app's settings on developer.paypal.com, make sure dispute access is ticked.")
        try:
            order = pp.create_order("1.00", "USD", "Rebuttal setup check", "CHECK")
            print(f"  ✓ Orders API works (created sandbox order {order['id']}, not paid)")
        except PayPalError as e:
            ok = False
            print(f"  ✕ Orders API refused: {e}")
        print("  Webhook id set" if os.environ.get("PAYPAL_WEBHOOK_ID") else
              "  · No PAYPAL_WEBHOOK_ID yet (fine for now: use 'Check PayPal now')")
    llm = LLM.from_env()
    print(f"Model: {llm.model if llm else 'none (template plans)'}")
    return 0 if ok else 1


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        sys.exit(check())
    print("usage: rebuttal check")
    sys.exit(2)


if __name__ == "__main__":
    main()
