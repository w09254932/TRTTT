"""Customer-facing pages: the invoice link, the hand-off to EdfaPay and its notifications."""
import json
import re

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from edfapay import GatewayError
from security import too_many
from validate import clean_email, clean_name, clean_phone

pub = Blueprint("pub", __name__)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{24}$")
CLEANERS = {"name": clean_name, "phone": clean_phone, "email": clean_email}
BUSY = "تعذر بدء الدفع الآن. انتظر لحظات ثم حاول مرة أخرى."


def svc():
    return current_app.extensions["svc"]


def _invoice(token):
    if too_many("public", 120):
        abort(429)
    if not TOKEN_RE.match(token):
        abort(404)
    inv = svc().store.by_token(token)
    if not inv:
        abort(404)
    if inv["overdue"]:
        svc().store.close(inv["id"], "expired")
        inv["status"] = "expired"
    return inv


def _page(inv, form=None, error=None, code=200):
    need = [field for field in CLEANERS if not inv["private"].get(field)]
    return render_template("pay.html", inv=inv, need=need, form=form or {}, error=error), code


@pub.get("/p/<token>")
def invoice(token):
    inv = _invoice(token)
    if inv["status"] == "pending":
        svc().store.add_view(inv)
    return _page(inv)


@pub.post("/p/<token>/pay")
def pay(token):
    inv = _invoice(token)
    if inv["status"] != "pending":
        return redirect(url_for(".invoice", token=token), 303)
    customer, filled = dict(inv["private"]), False
    for field, clean in CLEANERS.items():
        if not customer.get(field):
            value, error = clean(request.form.get(field, ""))
            if error:
                return _page(inv, request.form, error, 400)
            customer[field], filled = value, True
    order_id = svc().store.start_attempt(inv["id"], customer if filled else None)
    if not order_id:
        return _page(inv, request.form, BUSY, 429)
    back = f"{svc().cfg.base_url}/p/{token}/done"
    try:
        checkout = svc().gateway.initiate(order_id, inv["amount"], customer, back + "?r=ok", back + "?r=fail")
    except GatewayError as exc:
        current_app.logger.error("edfapay initiate failed for %s: %s", order_id, exc)
        inv["private"] = customer
        return _page(inv, None, BUSY, 502)
    return redirect(checkout, 303)


@pub.route("/p/<token>/done", methods=["GET", "POST"])
def done(token):
    """Where EdfaPay sends the customer back. The result shown comes only from the verified webhook."""
    inv = _invoice(token)
    tries = request.args.get("n", "0")
    tries = int(tries) if tries.isdigit() and len(tries) < 3 else 0
    waiting = inv["status"] == "pending" and request.args.get("r") != "fail" and tries < 12
    again = url_for(".done", token=token, r="ok", n=tries + 1)
    return render_template("done.html", inv=inv, waiting=waiting, again=again)


@pub.post("/webhook/edfapay")
def webhook():
    raw = request.get_data()
    if not svc().gateway.verify(raw, request.headers.get("X-EdfaPay-Signature", "")):
        current_app.logger.warning("webhook rejected: bad signature")
        return {"ok": False}, 401
    try:
        data = json.loads(raw)
        details = data.get("pgDetails")
        event = {
            "order_id": str(data.get("orderId") or "")[:64],
            "txn": str(data.get("transactionId") or "")[:64],
            "status": str(data.get("status") or "")[:20],
            "type": str(data.get("type") or "")[:20],
            "amount": round(float(data.get("amount") or 0), 2),
            "reason": str((details.get("reason") if isinstance(details, dict) else "") or "")[:200],
            "scheme": str(data.get("cardScheme") or "")[:30],
            "rrn": str(data.get("rrn") or "")[:30],
        }
    except (ValueError, TypeError, AttributeError):
        return {"ok": False}, 400
    result = svc().store.apply_webhook(event)
    current_app.logger.info("webhook %s %s for %s: %s", event["type"], event["status"], event["order_id"], result)
    return {"ok": True, "result": result}
