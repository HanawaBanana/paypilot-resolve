"""Deterministic local PayPal.

Same interface as the sandbox adapter, but with an injected clock, sequential
identifiers and scriptable failures, so the whole resolution flow (including
interruption and resume) is testable offline. Nothing here touches the network.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


class PayPalPlatformError(Exception):
    """A definitive business error returned by the platform."""


class PayPalTransportError(Exception):
    """Ambiguous outcome: the request may or may not have been accepted."""


class MockPayPal:
    def __init__(self, clock: Optional[Callable[[], float]] = None,
                 failures: Optional[List[str]] = None):
        self._clock = clock or (lambda: 0.0)
        self._failures = list(failures or [])     # e.g. ["capture:transport", "payout:business"]
        self._post_accept: List[str] = []         # accepted by the platform, then the link died
        self._seq = 0
        self.calls: List[Dict[str, Any]] = []     # every request, for assertions
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.captures: Dict[str, Dict[str, Any]] = {}
        self.refunds: Dict[str, Dict[str, Any]] = {}
        self.batches: Dict[str, Dict[str, Any]] = {}
        self.items: Dict[str, Dict[str, Any]] = {}
        self._by_request_id: Dict[str, str] = {}

    # ------------------------------------------------------------------ helpers
    def _id(self, prefix: str) -> str:
        self._seq += 1
        return "{}-{:04d}".format(prefix, self._seq)

    def _maybe_fail(self, kind: str) -> None:
        for token in list(self._failures):
            if token.startswith(kind + ":"):
                self._failures.remove(token)
                how = token.split(":", 1)[1]
                if how == "transport":
                    raise PayPalTransportError("simulated transport failure on " + kind)
                raise PayPalPlatformError("simulated business failure on " + kind)

    def fail_after_accept(self, kind: str) -> None:
        """Simulate: the platform accepted the write, the response never arrived."""
        self._post_accept.append(kind)

    def _maybe_fail_after(self, kind: str) -> None:
        if kind in self._post_accept:
            self._post_accept.remove(kind)
            raise PayPalTransportError("simulated post-accept transport failure on " + kind)

    def _replay(self, request_id: str, kind: str) -> Optional[Dict[str, Any]]:
        """Idempotent replay: the same request id never creates a second resource."""
        key = kind + "|" + request_id
        existing = self._by_request_id.get(key)
        if not existing:
            return None
        for store in (self.orders, self.captures, self.refunds, self.batches):
            if existing in store:
                return store[existing]
        return None

    # ------------------------------------------------------------------- writes
    def create_order(self, amount: str, currency: str, reference: str,
                     request_id: str) -> Dict[str, Any]:
        self.calls.append({"op": "create_order", "request_id": request_id,
                           "amount": amount, "reference": reference})
        replay = self._replay(request_id, "create_order")
        if replay:
            return replay
        self._maybe_fail("create_order")
        oid = self._id("ORDER")
        order = {"id": oid, "status": "CREATED", "amount": amount, "currency": currency,
                 "reference": reference, "request_id": request_id,
                 "approve_url": "https://mock.local/checkoutnow?token=" + oid}
        self.orders[oid] = order
        self._by_request_id["create_order|" + request_id] = oid
        self._maybe_fail_after("create_order")
        return dict(order)

    def buyer_approve(self, order_id: str) -> Dict[str, Any]:
        """Stands in for the buyer approving in the PayPal-hosted checkout."""
        self.orders[order_id]["status"] = "APPROVED"
        return dict(self.orders[order_id])

    def capture_order(self, order_id: str, request_id: str) -> Dict[str, Any]:
        self.calls.append({"op": "capture_order", "request_id": request_id,
                           "order_id": order_id})
        replay = self._replay(request_id, "capture_order")
        if replay:
            return replay
        order = self.orders.get(order_id)
        if not order:
            raise PayPalPlatformError("ORDER_NOT_FOUND")
        if order["status"] != "APPROVED":
            raise PayPalPlatformError("ORDER_NOT_APPROVED")
        self._maybe_fail("capture")
        cid = self._id("CAP")
        capture = {"id": cid, "order_id": order_id, "status": "COMPLETED",
                   "amount": order["amount"], "currency": order["currency"],
                   "request_id": request_id}
        self.captures[cid] = capture
        self.orders[order_id]["status"] = "COMPLETED"
        self._by_request_id["capture_order|" + request_id] = cid
        self._maybe_fail_after("capture")
        return {"id": order_id, "status": "COMPLETED", "capture": dict(capture)}

    def refund(self, capture_id: str, amount: str, currency: str,
               request_id: str) -> Dict[str, Any]:
        self.calls.append({"op": "refund", "request_id": request_id,
                           "capture_id": capture_id, "amount": amount})
        replay = self._replay(request_id, "refund")
        if replay:
            return replay
        capture = self.captures.get(capture_id)
        if not capture:
            raise PayPalPlatformError("CAPTURE_NOT_FOUND")
        if float(amount) > float(capture["amount"]) - self.refunded_minor(capture_id) / 100.0 + 1e-9:
            raise PayPalPlatformError("REFUND_EXCEEDS_CAPTURE")
        self._maybe_fail("refund")
        rid = self._id("RFD")
        rec = {"id": rid, "capture_id": capture_id, "status": "COMPLETED",
               "amount": amount, "currency": currency, "request_id": request_id}
        self.refunds[rid] = rec
        self._by_request_id["refund|" + request_id] = rid
        self._maybe_fail_after("refund")
        return dict(rec)

    def refunded_minor(self, capture_id: str) -> int:
        return sum(int(round(float(r["amount"]) * 100)) for r in self.refunds.values()
                   if r["capture_id"] == capture_id)

    def payout(self, receiver: str, amount: str, currency: str, sender_batch_id: str,
               sender_item_id: str, request_id: str) -> Dict[str, Any]:
        self.calls.append({"op": "payout", "request_id": request_id,
                           "receiver": receiver, "amount": amount,
                           "sender_batch_id": sender_batch_id,
                           "sender_item_id": sender_item_id})
        replay = self._replay(request_id, "payout")
        if replay:
            return replay
        self._maybe_fail("payout")
        bid = self._id("BATCH")
        iid = self._id("ITEM")
        batch = {"id": bid, "status": "SUCCESS", "request_id": request_id,
                 "sender_batch_id": sender_batch_id}
        item = {"id": iid, "batch_id": bid, "status": "SUCCESS", "amount": amount,
                "currency": currency, "receiver": receiver,
                "sender_item_id": sender_item_id, "request_id": request_id}
        self.batches[bid] = batch
        self.items[iid] = item
        self._by_request_id["payout|" + request_id] = bid
        self._maybe_fail_after("payout")
        return {"batch": dict(batch), "item": dict(item)}

    # -------------------------------------------------------------------- reads
    def find_by_request_id(self, request_id: str) -> Dict[str, Any]:
        for kind, store, key in (("create_order", self.orders, "order_id"),
                                 ("capture_order", self.captures, "capture_id"),
                                 ("refund", self.refunds, "refund_id"),
                                 ("payout", self.batches, "batch_id")):
            hit = self._by_request_id.get(kind + "|" + request_id)
            if hit:
                found = store.get(hit)
                if found:
                    return {"found": True, key: found["id"], "record": dict(found)}
        return {"found": False, "resource": None, "record": None}

    def get_capture(self, capture_id: str) -> Dict[str, Any]:
        rec = self.captures.get(capture_id)
        if not rec:
            raise PayPalPlatformError("CAPTURE_NOT_FOUND")
        return dict(rec)

    def get_refund(self, refund_id: str) -> Dict[str, Any]:
        rec = self.refunds.get(refund_id)
        if not rec:
            raise PayPalPlatformError("REFUND_NOT_FOUND")
        return dict(rec)

    def get_payout_batch(self, batch_id: str) -> Dict[str, Any]:
        rec = self.batches.get(batch_id)
        if not rec:
            raise PayPalPlatformError("BATCH_NOT_FOUND")
        return dict(rec)

    def get_payout_item(self, item_id: str) -> Dict[str, Any]:
        rec = self.items.get(item_id)
        if not rec:
            raise PayPalPlatformError("ITEM_NOT_FOUND")
        return dict(rec)

    def calls_for(self, operation_kind: str) -> int:
        return sum(1 for c in self.calls if c["op"] == operation_kind)
