"""Callback and payment confirmation for EdfaPay accounts that use a merchant ID and password.
The callback is only a signal: an invoice becomes paid when EdfaPay's status API confirms it."""
import hashlib
import hmac
import re
import time

from flask import Blueprint, abort, current_app, request

from edfapay import GatewayError
from security import too_many

legacy = Blueprint("legacy", __name__)
ORDER_RE = re.compile(r"^[A-Z2-9]{12}x\d{1,3}$")
PAYMENT_RE = re.compile(r"^[A-Za-z0-9-]{6,64}$")
PAID = ("applied", "already_paid", "duplicate")
_last_poll = {}


def svc():
    return current_app.extensions["svc"]


def callback_key(cfg):
    """Secret part of the callback link, so only EdfaPay (who was given the link) can call it."""
    return hmac.new(cfg.secret_key.encode(), b"edfapay-callback", hashlib.sha256).hexdigest()[:32]


def callback_url(cfg):
    return f"{cfg.base_url}/callback/edfapay/{callback_key(cfg)}"


def confirm(order_id, payment_id, hint=None):
    """Asks the status API about one payment and applies the answer. Returns what was done."""
    s, hint = svc(), hint or {}
    if not ORDER_RE.match(order_id or "") or not PAYMENT_RE.match(payment_id or ""):
        return "ignored"
    inv = s.store.get(order_id.partition("x")[0])
    if not inv:
        return "unknown_order"
    try:
        record = s.gateway.status(order_id, payment_id) or {}
    except GatewayError as exc:
        current_app.logger.error("edfapay status failed for %s: %s", order_id, exc)
        record = {}
    state = str(record.get("status") or "").lower()
    event = {"order_id": order_id, "txn": payment_id, "type": "Purchase", "amount": inv["amount"],
             "reason": "", "scheme": "", "rrn": ""}
    if state == "settled":
        order = record.get("order") if isinstance(record.get("order"), dict) else {}
        if str(order.get("number") or order_id) != order_id:
            current_app.logger.warning("edfapay status for %s names another order", order_id)
            return "order_mismatch"
        if inv["status"] in ("paid", "refunded"):
            return "already_paid"
        try:
            paid = round(float(order.get("amount") or inv["amount"]), 2)
        except (TypeError, ValueError):
            paid = -1.0
        event.update(status="Approved", amount=paid, scheme=str(record.get("brand") or "")[:30],
                     rrn=str(record.get("rrn") or "")[:30])
    elif hint.get("result") == "DECLINED":
        event.update(status="Declined", reason=str(hint.get("reason") or record.get("reason") or "")[:200])
    else:
        return "failed" if state == "txn_failure" else "not_confirmed"
    return s.store.apply_webhook(event)


def poll(inv):
    """While the customer waits on the result page: asks EdfaPay about the last checkout of this invoice."""
    last, moment = inv.get("checkout") or {}, time.time()
    if not last.get("id") or moment - _last_poll.get(inv["id"], 0) < 4:
        return None
    if len(_last_poll) > 5000:
        _last_poll.clear()
    _last_poll[inv["id"]] = moment
    return confirm(last.get("order"), last.get("id"))


@legacy.post("/callback/edfapay/<key>")
def callback(key):
    s = svc()
    if not hmac.compare_digest(key.encode(), callback_key(s.cfg).encode()):
        abort(404)
    if too_many("callback", 240):
        return "ERROR", 429
    data = request.form.to_dict() or request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    order_id, payment_id = str(data.get("order_id") or "")[:64], str(data.get("trans_id") or "")[:64]
    result, status, action = (str(data.get(k) or "").upper() for k in ("result", "status", "action"))
    outcome, known = "ignored", ORDER_RE.match(order_id) and PAYMENT_RE.match(payment_id)
    inv = s.store.get(order_id.partition("x")[0]) if known else None
    if inv:
        if result == "SUCCESS" and (status == "REFUND" or action in ("REFUND", "CREDITVOID")):
            try:
                amount = round(float(data.get("amount") or inv["amount"]), 2)
            except (TypeError, ValueError):
                amount = inv["amount"]
            outcome = s.store.apply_webhook({"order_id": order_id, "txn": payment_id, "status": "Approved",
                                             "type": "Refund", "amount": amount, "reason": "", "scheme": "", "rrn": ""})
        elif result in ("SUCCESS", "DECLINED"):
            s.store.note_checkout(order_id.partition("x")[0], order_id, payment_id)
            outcome = confirm(order_id, payment_id, {"result": result, "reason": data.get("decline_reason")})
    current_app.logger.info("callback %s %s %s for %s: %s", action, result, status, order_id or "-", outcome)
    waiting = result == "SUCCESS" and outcome in ("not_confirmed", "failed")
    return ("ERROR" if waiting else "OK"), (503 if waiting else 200), {"Content-Type": "text/plain"}
