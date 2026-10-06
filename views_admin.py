"""Owner-only pages: sign in, statistics, creating and following invoices."""
import math
from datetime import datetime, timezone
from urllib.parse import quote

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from edfapay import GatewayError
from security import check_csrf, check_owner, ip_key, sign_in, signed_in, too_many
from validate import clean_amount, clean_email, clean_name, clean_phone, clean_text

admin = Blueprint("admin", __name__)
VALID_DAYS = (1, 3, 7, 14, 30)


def svc():
    return current_app.extensions["svc"]


@admin.before_request
def guard():
    if request.endpoint != "admin.login" and not signed_in(svc().cfg):
        session.clear()
        return redirect(url_for("admin.login"))
    if request.method == "POST":
        check_csrf()


@admin.route("/login", methods=["GET", "POST"])
def login():
    s = svc()
    if signed_in(s.cfg):
        return redirect(url_for(".dashboard"))
    error, code = None, 200
    if request.method == "POST":
        address = ip_key(s.cfg)
        wait = s.store.lock_left(address, "all")
        if wait or too_many("login", 15):
            error, code = f"محاولات كثيرة. حاول بعد {math.ceil((wait or 60) / 60)} دقيقة.", 429
        elif check_owner(s.cfg, request.form.get("number", ""), request.form.get("password", "")):
            previous = s.store.login_ok(address)
            sign_in(s.cfg)
            session["prev"] = previous.isoformat() if previous else ""
            current_app.logger.info("owner signed in")
            return redirect(url_for(".dashboard"), 303)
        else:
            s.store.login_failed(address, 5)
            s.store.login_failed("all", 30)
            current_app.logger.warning("failed sign-in attempt")
            error, code = "بيانات الدخول غير صحيحة.", 401
    return render_template("login.html", error=error), code


@admin.post("/logout")
def logout():
    session.clear()
    return redirect(url_for(".login"), 303)


def _dashboard(form=None, error=None, code=200):
    s, before = svc(), None
    cursor = request.args.get("before", "")
    if cursor.isdigit() and len(cursor) < 18:
        before = datetime.fromtimestamp(int(cursor) / 1e6, timezone.utc)
    rows, more = s.store.recent(20, before)
    for inv in rows:
        if inv["overdue"]:
            s.store.close(inv["id"], "expired")
            inv["status"] = "expired"
    stats, days = s.store.stats(), s.store.daily(14)
    older = int(rows[-1]["created_at"].timestamp() * 1e6) if more else None
    previous = session.get("prev")
    return render_template(
        "dashboard.html", rows=rows, older=older, stats=stats, days=days, form=form or {}, error=error,
        net=stats["paid_amount"] - stats["refunded_amount"], valid_days=VALID_DAYS,
        rate=round(100 * (stats["paid"] + stats["refunded"]) / stats["links"]) if stats["links"] else 0,
        peak=max([d["created"] for d in days] + [d["paid"] for d in days] + [1]),
        previous=datetime.fromisoformat(previous) if previous else None), code


@admin.get("/")
def dashboard():
    return _dashboard()


@admin.post("/invoices")
def create():
    form = request.form
    amount, error = clean_amount(form.get("amount", ""))
    private = {"title": clean_text(form.get("title", ""), 120)}
    if not error and len(private["title"]) < 2:
        error = "اكتب وصفاً للفاتورة."
    for field, clean in (("name", clean_name), ("phone", clean_phone), ("email", clean_email)):
        if not error and form.get(field, "").strip():
            private[field], error = clean(form[field])
    days = int(form.get("days")) if form.get("days") in map(str, VALID_DAYS) else 7
    if error:
        return _dashboard(form, error, 400)
    inv_id = svc().store.create_invoice(amount, private, days)
    flash("تم إنشاء رابط الدفع.")
    return redirect(url_for(".invoice", inv_id=inv_id), 303)


@admin.get("/find")
def find():
    return redirect(url_for(".invoice", inv_id=clean_text(request.args.get("q", ""), 20).upper() or "0"))


def _load(inv_id):
    inv = svc().store.get(inv_id)
    if not inv:
        abort(404)
    if inv["overdue"]:
        svc().store.close(inv_id, "expired")
        inv["status"] = "expired"
    return inv


@admin.get("/invoices/<inv_id>")
def invoice(inv_id):
    inv = _load(inv_id)
    link = f"{svc().cfg.base_url}/p/{inv['token']}"
    text = quote(f"فاتورة {inv['private'].get('title', '')} بمبلغ {inv['amount']:,.2f} ر.س\n{link}")
    phone = inv["private"].get("phone", "").lstrip("+")
    return render_template("invoice.html", inv=inv, link=link, events=svc().store.events_for(inv_id),
                           whatsapp=f"https://wa.me/{phone}?text={text}")


@admin.post("/invoices/<inv_id>/cancel")
def cancel(inv_id):
    _load(inv_id)
    done = svc().store.close(inv_id, "cancelled")
    flash("تم إلغاء الفاتورة." if done else "لا يمكن إلغاء هذه الفاتورة لأنها لم تعد مفتوحة.")
    return redirect(url_for(".invoice", inv_id=inv_id), 303)


@admin.post("/invoices/<inv_id>/check")
def check(inv_id):
    """Asks EdfaPay directly about the last transaction. Shown for information; it changes nothing."""
    inv = _load(inv_id)
    try:
        record = svc().gateway.status(inv["txn"]) if inv.get("txn") else None
    except GatewayError as exc:
        current_app.logger.error("edfapay status failed for %s: %s", inv_id, exc)
        record = None
    if record:
        parts = [str(record.get(k) or "-") for k in ("transactionStatus", "paymentStatus", "refundStatus")]
        flash("رد البوابة: الحالة {} / الدفع {} / الاسترداد {}".format(*parts)
              + (f" / السبب: {record['declineReason']}" if record.get("declineReason") else ""))
    else:
        flash("لم يصل رد من البوابة عن هذه العملية.")
    return redirect(url_for(".invoice", inv_id=inv_id), 303)
