"""Owner-only pages: sign in, statistics, creating and following invoices."""
import math
from datetime import datetime, timezone
from urllib.parse import quote

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

import legacy_flow
import notices
from edfapay import GatewayError
from mailer import hint
from security import check_csrf, check_owner, ip_key, sign_in, signed_in, too_many
from validate import clean_amount, clean_email, clean_goods, clean_name, clean_phone, clean_text

admin = Blueprint("admin", __name__)
VALID_DAYS = (1, 3, 7, 14, 30)


def svc():
    return current_app.extensions["svc"]


def _current(s):
    """False for a session made before the email sign-in code was turned on (it signs older sessions out)."""
    mark = session.get("e", 0)
    return mark == s.codes.epoch() or mark == s.codes.epoch(fresh=True)


@admin.before_request
def guard():
    s = svc()
    if request.endpoint != "admin.login" and not (signed_in(s.cfg) and _current(s)):
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
            import twostep
            if twostep.needed(s):
                return twostep.begin(s)
            finish_login(s, address)
            current_app.logger.info("owner signed in")
            return redirect(url_for(".dashboard"), 303)
        else:
            s.store.login_failed(address, 5)
            s.store.login_failed("all", 30)
            current_app.logger.warning("failed sign-in attempt")
            error, code = "بيانات الدخول غير صحيحة.", 401
    return render_template("login.html", error=error), code


def finish_login(s, address):
    """The owner is who they say they are: start the signed-in session."""
    previous = s.store.login_ok(address)
    sign_in(s.cfg)
    session["prev"] = previous.isoformat() if previous else ""
    session["e"] = s.codes.epoch(fresh=True)


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
    people = s.book.recent(50)
    chosen = (form or {}).get("customer") or request.args.get("customer", "")
    if chosen and chosen not in [p["id"] for p in people]:
        people = [p for p in [s.book.get(chosen)] if p] + people
    older = int(rows[-1]["created_at"].timestamp() * 1e6) if more else None
    previous = session.get("prev")
    return render_template(
        "dashboard.html", rows=rows, older=older, stats=stats, days=days, form=form or {}, error=error,
        net=stats["paid_amount"] - stats["refunded_amount"], valid_days=VALID_DAYS, mailing=s.mailer.ready,
        callback=legacy_flow.callback_url(s.cfg) if s.cfg.legacy else s.cfg.base_url + "/webhook/edfapay",
        people=people, chosen=chosen,
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
    goods, goods_error = clean_goods(form.get("goods", ""))
    error = error or goods_error
    saved = svc().book.get(form.get("customer", "")) or {}
    for field, clean in (("name", clean_name), ("phone", clean_phone), ("email", clean_email)):
        value = form.get(field, "").strip() or saved.get(field, "")
        if not error and value:
            private[field], error = clean(value)
    days = int(form.get("days")) if form.get("days") in map(str, VALID_DAYS) else 7
    if error:
        return _dashboard(form, error, 400)
    inv_id = svc().store.create_invoice(amount, private, days, svc().book.remember(private), goods)
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
                           whatsapp=f"https://wa.me/{phone}?text={text}", goods=svc().store.goods(inv),
                           mailing=svc().mailer.ready, delivery=inv.get("delivery") or {})


@admin.post("/invoices/<inv_id>/goods")
def save_goods(inv_id):
    _load(inv_id)
    text, error = clean_goods(request.form.get("goods", ""))
    if error:
        flash(error, "error")
    elif not svc().store.set_goods(inv_id, text):
        flash("لا يمكن تعديل الطلب لأن الفاتورة لم تعد مفتوحة.", "error")
    else:
        flash("تم حفظ الطلب الذي يُرسل للعميل." if text else "تم حذف الطلب الذي يُرسل للعميل.")
    return redirect(url_for(".invoice", inv_id=inv_id), 303)


@admin.post("/invoices/<inv_id>/deliver")
def send_goods(inv_id):
    """Sends the order to the customer now, also again after an earlier email."""
    inv = _load(inv_id)
    if too_many("deliver", 10):
        abort(429)
    if inv["status"] != "paid" or not inv["has_goods"]:
        flash("يُرسل الطلب بعد دفع الفاتورة فقط، ويجب أن يكون مكتوباً.", "error")
        return redirect(url_for(".invoice", inv_id=inv_id), 303)
    to = request.form.get("email", "").strip().lower()
    if to and to != inv["deliver_email"]:
        to, error = clean_email(to)
        if error:
            flash(error, "error")
            return redirect(url_for(".invoice", inv_id=inv_id), 303)
        svc().store.set_delivery_email(inv_id, to)
        inv["deliver_email"] = to
    state, reason = notices.deliver(svc(), inv_id, again=True)
    if state == "sent":
        flash(f"أُرسل الطلب إلى {inv['deliver_email']}.")
    elif state == "failed":
        flash(f"لم يُرسل الطلب: {hint(reason)}", "error")
    else:
        flash("الطلب يُرسل الآن. حدّث الصفحة بعد لحظات.")
    return redirect(url_for(".invoice", inv_id=inv_id), 303)


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
    if svc().gateway.legacy:
        last = inv.get("checkout") or {}
        outcome = legacy_flow.confirm(last.get("order"), last.get("id"))
        if outcome == "applied":
            flash("أكدت البوابة الدفع وتم تحديث الفاتورة.")
        elif outcome in legacy_flow.PAID:
            flash("البوابة تؤكد أن هذه الفاتورة مدفوعة.")
        elif outcome == "amount_mismatch":
            flash("المبلغ المدفوع في البوابة لا يطابق مبلغ الفاتورة، فلم تُحتسب مدفوعة.")
        else:
            flash("البوابة لم تؤكد دفع هذه الفاتورة حتى الآن.")
        return redirect(url_for(".invoice", inv_id=inv_id), 303)
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
