"""«Tell me when it is back»: a signed-in customer asks about a sold-out product, and gets one email when the owner
adds codes to it. Only the account's id is kept with the product, never the email itself."""
from flask import Blueprint, abort, current_app, flash, redirect, request, url_for
from google.cloud import firestore

from accounts import signed_in_customer
from security import check_csrf, too_many

restock = Blueprint("restock", __name__)


def svc():
    return current_app.extensions["svc"]


def _doc(pid):
    return svc().store.db.collection("restock").document(pid)


def waiting_for(pid, me):
    """True when this customer already asked about this product."""
    if not me:
        return False
    snap = _doc(pid).get()
    return snap.exists and me["id"] in ((snap.to_dict() or {}).get("who") or [])


@restock.post("/notify/<pid>")
def ask(pid):
    check_csrf()
    if too_many("restock", 20, per=600):
        abort(429)
    from products import PID_RE
    product = svc().shop.get(pid) if PID_RE.match(pid) else None
    if not product or not product["active"]:
        abort(404)
    me = signed_in_customer()
    if not me:
        flash("سجّل الدخول لنبلغك حين يتوفر المنتج.", "error")
        return redirect(url_for("account.login", next=url_for("shop.store")), 303)
    if product["available"] > 0:
        flash(f"«{product['name']}» متوفر الآن.")
        return redirect(url_for("shop.store"), 303)
    _doc(pid).set({"who": firestore.ArrayUnion([me["id"]])}, merge=True)
    flash(f"سنرسل لك إيميلاً على {me['email']} حين يتوفر «{product['name']}».")
    return redirect(url_for("shop.store"), 303)


def notify(s, pid):
    """Called after codes are added: each customer who asked gets one email, then the list is cleared.
    The emails go out from the mail thread. Returns how many were asked."""
    product = s.shop.get(pid)
    if not product or not product["active"] or product["available"] <= 0 or not s.mailer.ready:
        return 0
    ref = s.store.db.collection("restock").document(pid)
    snap = ref.get()
    who = list((snap.to_dict() or {}).get("who") or []) if snap.exists else []
    if not who:
        return 0
    ref.delete()
    s.mailer.later(_send_all, current_app._get_current_object(), pid, who)
    return len(who)


def _send_all(app, pid, who):
    from notices import _send, message
    with app.app_context():
        s = app.extensions["svc"]
        product = s.shop.get(pid)
        if not product:
            return
        for aid in who:
            account = s.accounts.get(aid)
            if not account or not account.get("email"):
                continue
            mail = message(s, f"«{product['name']}» متوفر الآن في {s.cfg.store_name}", "توفر المنتج الذي طلبته",
                           f"طلبت أن نبلغك حين يتوفر «{product['name']}»، وهو متوفر الآن.",
                           rows=[("المنتج", product["name"]), ("السعر", f"{product['price']:,.2f} ر.س")],
                           button=("اشترِ الآن", f"{s.cfg.base_url}/"), logo=True,
                           outro="أرسلنا لك هذه الرسالة مرة واحدة لأنك طلبتها.")
            error = _send(s, account["email"], mail)
            if error:
                app.logger.warning("restock email failed for %s: %s", pid, error)
