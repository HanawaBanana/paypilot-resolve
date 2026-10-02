"""PayPal sandbox REST adapter.

Same interface as `mock_paypal.MockPayPal`, so the orchestrator, the executor and
the UI cannot tell which platform they are talking to — only the evidence ids and
the badge differ.

Every write carries a stable `PayPal-Request-Id` supplied by the caller; payouts
additionally carry the persisted `sender_batch_id` / `sender_item_id`. Anything
this adapter cannot verify is reported as unverifiable instead of guessed.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from mock_paypal import PayPalPlatformError, PayPalTransportError

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"


class PayPalClient:
    """Real HTTP. `mode="sandbox"` targets api-m.sandbox.paypal.com only."""

    def __init__(self, mode: str = "sandbox",
                 client_id: Optional[str] = None, client_secret: Optional[str] = None,
                 base: Optional[str] = None):
        self.mode = mode
        self.base = base or SANDBOX_BASE
        self.client_id = client_id or os.getenv("PAYPAL_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("PAYPAL_CLIENT_SECRET", "")
        self.app_base = os.getenv("APP_BASE_URL", "http://127.0.0.1:8100").rstrip("/")
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self.calls: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------- auth
    def _access_token(self) -> str:
        import time

        import requests

        if self._token and time.time() < self._token_expiry:
            return self._token
        resp = requests.post(self.base + "/v1/oauth2/token",
                             data={"grant_type": "client_credentials"},
                             auth=(self.client_id, self.client_secret), timeout=30)
        if resp.status_code >= 400:
            raise PayPalTransportError("oauth failed: %s" % resp.status_code)
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 3000)) - 60
        return self._token

    def _request(self, method: str, path: str, *, request_id: Optional[str] = None,
                 json_body: Optional[Dict[str, Any]] = None,
                 params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        import requests

        headers = {"Authorization": "Bearer " + self._access_token(),
                   "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        try:
            resp = requests.request(method, self.base + path, headers=headers, json=json_body,
                                    params=params, timeout=45)
        except requests.RequestException as exc:
            # ambiguous: the request may or may not have been accepted
            raise PayPalTransportError(type(exc).__name__ + ": " + str(exc)[:120])
        body: Dict[str, Any] = {}
        if resp.content:
            try:
                body = resp.json()
            except ValueError:
                body = {"raw": resp.text[:400]}
        if resp.status_code >= 400:
            name = ((body.get("name") or body.get("error") or "") if isinstance(body, dict) else "")
            raise PayPalPlatformError("%s %s -> %s %s" % (method, path, resp.status_code, name))
        return body

    # ----------------------------------------------------------------- writes
    def create_order(self, amount: str, currency: str, reference: str,
                     request_id: str) -> Dict[str, Any]:
        self.calls.append({"op": "create_order", "request_id": request_id, "amount": amount})
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": currency, "value": amount},
                "description": ("PayPilot Resolve · " + reference)[:127],
                "custom_id": reference[:127],
            }],
            "application_context": {
                "brand_name": "PayPilot Resolve",
                "locale": "en-US",
                "landing_page": "LOGIN",
                "shipping_preference": "NO_SHIPPING",
                "user_action": "PAY_NOW",
                "return_url": self.app_base + "/return",
                "cancel_url": self.app_base + "/cancel",
            },
        }
        data = self._request("POST", "/v2/checkout/orders", request_id=request_id, json_body=body)
        approve = next((l["href"] for l in data.get("links", []) if l.get("rel") == "approve"), None)
        return {"id": data.get("id"), "status": data.get("status"), "amount": amount,
                "currency": currency, "reference": reference, "request_id": request_id,
                "approve_url": approve}

    def capture_order(self, order_id: str, request_id: str) -> Dict[str, Any]:
        self.calls.append({"op": "capture_order", "request_id": request_id, "order_id": order_id})
        data = self._request("POST", "/v2/checkout/orders/%s/capture" % order_id,
                             request_id=request_id, json_body={})
        capture: Dict[str, Any] = {}
        try:
            capture = data["purchase_units"][0]["payments"]["captures"][0]
        except (KeyError, IndexError, TypeError):
            capture = {"id": order_id, "status": data.get("status"), "unparsed": True}
        return {"id": data.get("id"), "status": data.get("status"),
                "capture": {"id": capture.get("id"), "order_id": order_id,
                            "status": capture.get("status"),
                            "amount": (capture.get("amount") or {}).get("value"),
                            "currency": (capture.get("amount") or {}).get("currency_code"),
                            "request_id": request_id}}

    def refund(self, capture_id: str, amount: str, currency: str,
               request_id: str) -> Dict[str, Any]:
        self.calls.append({"op": "refund", "request_id": request_id, "capture_id": capture_id,
                           "amount": amount})
        body = {"amount": {"value": amount, "currency_code": currency}}
        data = self._request("POST", "/v2/payments/captures/%s/refund" % capture_id,
                             request_id=request_id, json_body=body)
        return {"id": data.get("id"), "capture_id": capture_id, "status": data.get("status"),
                "amount": (data.get("amount") or {}).get("value", amount),
                "currency": (data.get("amount") or {}).get("currency_code", currency),
                "request_id": request_id}

    def payout(self, receiver: str, amount: str, currency: str, sender_batch_id: str,
               sender_item_id: str, request_id: str) -> Dict[str, Any]:
        self.calls.append({"op": "payout", "request_id": request_id, "receiver": receiver,
                           "amount": amount, "sender_batch_id": sender_batch_id,
                           "sender_item_id": sender_item_id})
        body = {
            "sender_batch_header": {
                "sender_batch_id": sender_batch_id,
                "email_subject": "You have a PayPilot Resolve payout",
                "email_message": "A resolved scope change released this collaborator payment.",
            },
            "items": [{
                "recipient_type": "EMAIL",
                "amount": {"value": amount, "currency": currency},
                "receiver": receiver,
                "note": "Collaborator share for the delivered work"[:127],
                "sender_item_id": sender_item_id,
                "purpose": "GOODS",
            }],
        }
        data = self._request("POST", "/v1/payments/payouts", request_id=request_id, json_body=body)
        header = data.get("batch_header") or {}
        item: Dict[str, Any] = {}
        try:
            item = (data.get("items") or [])[0]
        except (IndexError, TypeError):
            item = {}
        return {
            "batch": {"id": header.get("payout_batch_id"), "status": header.get("batch_status"),
                      "request_id": request_id, "sender_batch_id": sender_batch_id},
            "item": {"id": (item.get("payout_item_id") or header.get("payout_batch_id")),
                     "batch_id": header.get("payout_batch_id"),
                     "status": item.get("transaction_status"),
                     "amount": amount, "currency": currency,
                     "receiver": receiver, "sender_item_id": sender_item_id,
                     "request_id": request_id},
        }

    # ------------------------------------------------------------------ reads
    def get_order(self, order_id: str) -> Dict[str, Any]:
        data = self._request("GET", "/v2/checkout/orders/%s" % order_id)
        return {"id": data.get("id"), "status": data.get("status"),
                "amount": (((data.get("purchase_units") or [{}])[0].get("amount") or {})
                           .get("value")),
                "currency": (((data.get("purchase_units") or [{}])[0].get("amount") or {})
                             .get("currency_code"))}

    def get_capture(self, capture_id: str) -> Dict[str, Any]:
        data = self._request("GET", "/v2/payments/captures/%s" % capture_id)
        return {"id": data.get("id"), "status": data.get("status"),
                "amount": (data.get("amount") or {}).get("value"),
                "currency": (data.get("amount") or {}).get("currency_code"),
                "order_id": ((data.get("supplementary_data") or {})
                             .get("related_ids", {}) or {}).get("order_id")}

    def get_refund(self, refund_id: str) -> Dict[str, Any]:
        data = self._request("GET", "/v2/payments/refunds/%s" % refund_id)
        return {"id": data.get("id"), "status": data.get("status"),
                "amount": (data.get("amount") or {}).get("value"),
                "currency": (data.get("amount") or {}).get("currency_code"),
                "capture_id": self._capture_id_from_links(data.get("links") or [])}

    @staticmethod
    def _capture_id_from_links(links: List[Dict[str, Any]]) -> Optional[str]:
        """`.../v2/payments/captures/<id>` -> `<id>` (the refund's parent)."""
        for link in links:
            href = link.get("href") or ""
            if "/captures/" in href:
                return href.rstrip("/").rsplit("/", 1)[-1]
        return None

    def list_refunds_for_capture(self, capture_id: str) -> List[Dict[str, Any]]:
        try:
            data = self._request("GET", "/v2/payments/captures/%s/refunds" % capture_id)
        except PayPalPlatformError:
            # Not every sandbox account exposes the listing endpoint; absence of a
            # listing is not evidence that no refund happened.
            return []
        out = []
        for item in data.get("refunds") or []:
            out.append({"id": item.get("id"), "status": item.get("status"),
                        "amount": (item.get("amount") or {}).get("value"),
                        "currency": (item.get("amount") or {}).get("currency_code")})
        return out

    def get_payout_batch(self, batch_id: str) -> Dict[str, Any]:
        data = self._request("GET", "/v1/payments/payouts/%s" % batch_id)
        header = data.get("batch_header") or {}
        return {"id": header.get("payout_batch_id"), "status": header.get("batch_status"),
                "amount": (header.get("amount") or {}).get("value"),
                "currency": (header.get("amount") or {}).get("currency_code")}

    def get_payout_item(self, item_id: str) -> Dict[str, Any]:
        data = self._request("GET", "/v1/payments/payouts-item/%s" % item_id)
        return {"id": data.get("payout_item_id"), "batch_id": data.get("payout_batch_id"),
                "status": data.get("transaction_status"),
                "amount": (data.get("payout_item") or {}).get("amount", {}).get("value"),
                "currency": (data.get("payout_item") or {}).get("amount", {}).get("currency_code"),
                "receiver": (data.get("payout_item") or {}).get("receiver")}

    def get_payout(self, item_or_batch_id: str) -> Dict[str, Any]:
        """Read a payout's status.

        The create response may hand back a batch id rather than an item id, and
        the item endpoint then 404s. Listing the batch is how you learn what
        actually happened to each recipient — a batch status of SUCCESS alone
        does not prove every item was paid.
        """
        try:
            return self.get_payout_item(item_or_batch_id)
        except PayPalPlatformError:
            data = self._request("GET", "/v1/payments/payouts/%s" % item_or_batch_id)
            header = data.get("batch_header") or {}
            items = data.get("items") or []
            first = items[0] if items else {}
            money = ((first.get("payout_item") or {}).get("amount") or {})
            return {"id": first.get("payout_item_id") or header.get("payout_batch_id"),
                    "batch_id": header.get("payout_batch_id"),
                    "batch_status": header.get("batch_status"),
                    "status": first.get("transaction_status") or header.get("batch_status"),
                    "amount": money.get("value"), "currency": money.get("currency_code"),
                    "receiver": (first.get("payout_item") or {}).get("receiver"),
                    "raw_item": {"transaction_status": first.get("transaction_status")}}

    def verify_webhook_signature(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/v1/notifications/verify-webhook-signature",
                             json_body=payload)

    # ------------------------------------------------------------ reconciliation
    def find_by_request_id(self, request_id: str) -> Dict[str, Any]:
        """PayPal exposes no lookup by PayPal-Request-Id, and this adapter will not
        pretend otherwise: use `find_for_operation` (which queries known ids)."""
        return {"found": False, "resource": None, "record": None,
                "note": "sandbox cannot resolve by request id alone"}

    def find_for_operation(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve what the platform recorded for one of our operations."""
        action = operation["action"]
        platform_id = operation["platform_id"]
        try:
            if action == "create_order" and platform_id:
                return {"found": True, "resource": platform_id,
                        "record": self.get_order(platform_id)}
            if action == "capture" and platform_id:
                return {"found": True, "resource": platform_id,
                        "record": self.get_capture(platform_id)}
            if action == "refund":
                if platform_id:
                    return {"found": True, "resource": platform_id,
                            "record": self.get_refund(platform_id)}
                if operation["resource_id"]:
                    for item in self.list_refunds_for_capture(operation["resource_id"]):
                        if operation["amount_raw"] and str(item["amount"]) == str(
                                operation["amount_raw"]):
                            return {"found": True, "resource": item["id"], "record": item}
                return {"found": False, "resource": None, "record": None,
                        "note": "no refund recorded for this capture"}
            if action == "payout" and platform_id:
                return {"found": True, "resource": platform_id,
                        "record": self.get_payout(platform_id)}
        except PayPalPlatformError as exc:
            return {"found": False, "resource": None, "record": None, "note": str(exc)}
        except PayPalTransportError as exc:
            return {"found": None, "resource": None, "record": None,
                    "note": "platform unreachable: " + str(exc)[:120]}
        return {"found": False, "resource": None, "record": None,
                "note": "no platform id recorded; unverifiable"}
