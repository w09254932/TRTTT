"""Owner-only pages: sign in, statistics, creating and following invoices."""
import math
from datetime import datetime, timedelta, timezone
from functools import partial
from urllib.parse import quote

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

import legacy_flow
import notices
from edfapay import GatewayError
from mailer import hint
from phones import clean_phone
from security import check_csrf, check_owner, ip_key, sign_in, signed_in, too_many
from validate import clean_amount, clean_email, clean_goods, clean_name, clean_text

admin = Blueprint("admin", __name__, url_prefix="/admin")
VALID_DAYS = (1, 3, 7, 14, 30)
FILTERS = (("", "الكل"), ("pending", "بانتظار الدفع"), ("paid", "مدفوعة"), ("ended", "منتهية"), ("refunded", "مستردة"))
PAGE = 10
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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
    group = request.args.get("status", "")
    group = group if group in dict(FILTERS) else ""
    cursor = request.args.get("before", "")
    if cursor.isdigit() and len(cursor) < 18:
        before = EPOCH + timedelta(microseconds=int(cursor))
    # Nothing created before the longest link validity can still be waiting to be paid.
    since = datetime.now(timezone.utc) - timedelta(days=max(VALID_DAYS) + 1) if group == "pending" else None
    rows, oldest = s.store.page(group or None, PAGE, before, since=since)
    for inv in rows:
        if inv["overdue"]:
            s.store.close(inv["id"], "expired")
            inv["status"] = "expired"
    stats, days = s.store.stats(), s.store.daily(14)
    people = s.book.recent(50)
    chosen = (form or {}).get("customer") or request.args.get("customer", "")
    if chosen and chosen not in [p["id"] for p in people]:
        people = [p for p in [s.book.get(chosen)] if p] + people
    older = (oldest - EPOCH) // timedelta(microseconds=1) if oldest else None
    previous = session.get("prev")
    return render_template(
        "dashboard.html", rows=rows, older=older, newest=before is None, status=group, stats=stats, days=days,
        filters=[f for f in FILTERS if f[0] != "refunded" or stats["refunded"] or group == "refunded"],
        form=form or {}, error=error,
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
    phone = partial(clean_phone, code=form.get("cc"))
    for field, clean in (("name", clean_name), ("phone", phone), ("email", clean_email)):
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
    product = svc().shop.get(inv["product"]) if inv.get("product") else None
    missing = svc().shop.missing(inv) if inv.get("product") and inv["status"] == "paid" else 0
    return render_template("invoice.html", inv=inv, link=link, events=svc().store.events_for(inv_id),
                           whatsapp=f"https://wa.me/{phone}?text={text}", goods=svc().store.goods(inv),
                           mailing=svc().mailer.ready, delivery=inv.get("delivery") or {}, product=product,
                           missing=missing)


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


@admin.post("/invoices/<inv_id>/stock")
def from_stock(inv_id):
    """A paid shop order that found no code when it was paid: hands one out now and emails it."""
    inv = _load(inv_id)
    outcome = svc().shop.assign(inv_id) if inv.get("product") else "none"
    if outcome == "empty":
        flash("لا يوجد كود في مخزون هذا المنتج. أضف أكواداً من صفحة المتجر ثم حاول مرة أخرى.", "error")
    elif outcome in ("assigned", "already", "partial"):
        state, reason = notices.deliver(svc(), inv_id, again=True) if svc().mailer.ready else (None, "")
        if outcome == "partial":
            flash("أُخذ ما في المخزون، وما زالت أكواد ناقصة. أضف أكواداً ثم اضغط مرة أخرى.", "error")
        elif state == "sent":
            flash("أُخذ كود من المخزون وأُرسل إلى بريد العميل.")
        elif state == "failed":
            flash(f"أُخذ كود من المخزون لكن لم يُرسل: {hint(reason)}", "error")
        else:
            flash("أُخذ كود من المخزون. تجده في الطلب أدناه.")
    else:
        flash("هذه الفاتورة ليست طلباً مدفوعاً من المتجر.", "error")
    return redirect(url_for(".invoice", inv_id=inv_id), 303)


@admin.post("/invoices/<inv_id>/cancel")
def cancel(inv_id):
    inv = _load(inv_id)
    done = svc().store.close(inv_id, "cancelled")
    if done and inv.get("product"):
        svc().shop.release(inv)
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
