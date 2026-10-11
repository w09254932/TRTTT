"""The emails: the customer's order once EdfaPay has confirmed the payment, a notice to the owner for every
payment, the owner's sign-in code and a test message. Payment emails go out once, from a mail thread."""
import time

from flask import current_app, render_template

from db import KSA, now
from mailer import MailError, one_line

RETRY_WAITS = (5, 20)  # seconds between automatic attempts while the mail server cannot be reached
SENT_TEXT = {"sent": "أُرسل إلى بريد العميل", "failed": "تعذر الإرسال. افتح الفاتورة وأعد الإرسال"}
LATE_TEXT = "لم يُرسل تلقائياً لأن الفاتورة كانت {} قبل الدفع. أرسله من صفحة الفاتورة إن كان ذلك صحيحاً"
CLOSED_AR = {"cancelled": "ملغاة", "expired": "منتهية"}


def _luminance(color):
    parts = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def message(s, subject, heading, intro="", rows=(), box="", box_label="", big=False, button=None, outro="",
            logo=False):
    """(subject, text, html) for one email. Both versions are built from the same parts and say the same thing."""
    look = s.brand.get()
    color = look["primary"]
    logo_url = f"{s.cfg.base_url}/brand/logo?v={look['version']}" if logo and look["logo"] else ""
    rows = [(label, one_line(value, 200)) for label, value in rows if value]
    html = render_template("mail.html", subject=subject, store=s.cfg.store_name, heading=heading, intro=intro,
                           rows=rows, box=box, box_label=box_label, big=big, button=button, outro=outro,
                           logo=logo_url, color=color, on_color="#14213d" if _luminance(color) > 0.35 else "#ffffff")
    text = [heading, ""]
    if intro:
        text += [intro, ""]
    if box:
        text += ([box_label + ":"] if box_label else []) + [box, ""]
    if rows:
        text += [f"{label}: {value}" for label, value in rows] + [""]
    if button:
        text += [f"{button[0]}: {button[1]}", ""]
    if outro:
        text += [outro, ""]
    text += ["--", f"رسالة آلية من {s.cfg.store_name}."]
    return subject, "\n".join(text) + "\n", html


def _send(s, to, mail, waits=()):
    """Sends, trying again only while the message certainly did not leave (the server could not be reached).
    Returns '' or why it failed."""
    error = "failed"
    for pause in (0, *waits):
        time.sleep(pause)
        try:
            s.mailer.send(to, *mail)
            return ""
        except MailError as exc:
            error = exc.kind
            if exc.kind not in ("connect", "failed"):
                break
    return error


# ---- after a payment
def settle(s, event):
    """Applies one confirmed gateway event; when it makes an invoice paid, the payment emails follow."""
    result = s.store.apply_webhook(event)
    if result == "applied" and event.get("status") == "Approved" and event.get("type") in ("Purchase", "Capture"):
        try:
            after_payment(s, event["order_id"].partition("x")[0])
        except Exception as exc:  # an email problem must never block or undo a confirmed payment
            current_app.logger.error("payment emails not queued for %s: %s", event["order_id"], type(exc).__name__)
    return result


def after_payment(s, inv_id):
    if s.mailer.ready:
        s.mailer.later(_paid, current_app._get_current_object(), inv_id)


def _paid(app, inv_id):
    with app.app_context():
        s, state = app.extensions["svc"], None
        try:
            state, _ = deliver(s, inv_id, waits=RETRY_WAITS)
        except Exception as exc:
            app.logger.error("order email failed for %s: %s", inv_id, type(exc).__name__)
            state = "failed"
        owner_notice(s, inv_id, state)


def deliver(s, inv_id, again=False, waits=()):
    """Emails the order of a paid invoice to its customer. Returns (state, reason): ('sent', ''),
    ('failed', why) or (None, '') when there is nothing to send now (no order, unpaid, sent or being sent)."""
    inv = s.store.claim_delivery(inv_id, again)
    if not inv:
        return None, ""
    tries, error = inv["delivery"]["tries"], "failed"
    try:
        goods, to = s.store.goods(inv), inv["deliver_email"]
        if not to or not goods:
            error = "address" if not to else "failed"
        else:
            title = inv["private"].get("title") or "طلبك"
            mail = message(s, f"طلبك من {s.cfg.store_name}: {title}", "تم الدفع، وهذا طلبك",
                           f"شكراً لك. استلمنا دفعتك لفاتورة رقم {inv['id']}.",
                           rows=[("الطلب", title), ("المبلغ", f"{inv['amount']:,.2f} ر.س")], box=goods,
                           box_label="طلبك", logo=True,
                           outro=f"احتفظ بهذه الرسالة. إن واجهت مشكلة في طلبك فتواصل مع {s.cfg.store_name}.")
            error = _send(s, to, mail, waits)
    finally:  # whatever happens, the invoice never stays marked as "being sent"
        s.store.finish_delivery(inv_id, tries, "failed" if error else "sent", error)
    if error:
        current_app.logger.warning("order email failed for %s: %s", inv_id, error)
        return "failed", error
    current_app.logger.info("order emailed for %s", inv_id)
    return "sent", ""


def owner_notice(s, inv_id, delivered=None):
    """Tells the owner about a payment, once per invoice."""
    if not s.cfg.owner_email or not s.store.claim_notice(inv_id):
        return
    error = "failed"
    try:
        inv = s.store.get(inv_id)
        title, amount = inv["private"].get("title") or "بدون وصف", f"{inv['amount']:,.2f} ر.س"
        paid_at = (inv.get("paid_at") or now()).astimezone(KSA).strftime("%Y-%m-%d %H:%M")
        rows = [("المبلغ", amount), ("الفاتورة", title), ("رقم الفاتورة", inv["id"]),
                ("العميل", inv["private"].get("name")), ("البطاقة", inv.get("scheme")), ("وقت الدفع", paid_at)]
        before = inv.get("paid_from", "pending")
        if inv.get("goods"):
            late = LATE_TEXT.format(CLOSED_AR.get(before, before)) if before != "pending" else ""
            rows.append(("تسليم الطلب", late or SENT_TEXT.get(delivered, "جارٍ الإرسال")))
        heading = "وصلتك دفعة جديدة" if before == "pending" else f"وصلتك دفعة لفاتورة {CLOSED_AR.get(before, before)}"
        mail = message(s, f"دفعة جديدة {amount}: {title}", heading, rows=rows,
                       button=("فتح الفاتورة", f"{s.cfg.base_url}/invoices/{inv['id']}"))
        error = _send(s, s.cfg.owner_email, mail, RETRY_WAITS)
    finally:
        s.store.finish_notice(inv_id, error)
    if error:
        current_app.logger.warning("payment notice failed for %s: %s", inv_id, error)
    else:
        current_app.logger.info("payment notice sent for %s", inv_id)


# ---- owner
def code_message(s, code, purpose, address):
    moment = now().astimezone(KSA).strftime("%Y-%m-%d %H:%M")
    if purpose == "enable":
        return message(s, f"رمز تفعيل الدخول بالإيميل: {code}", "تأكيد رمز الدخول",
                       "اكتب هذا الرمز في صفحة الإعدادات ليبدأ طلبه عند كل دخول إلى اللوحة. صالح 10 دقائق.",
                       box=code, big=True, rows=[("الوقت", moment)])
    return message(s, f"رمز الدخول إلى {s.cfg.store_name}: {code}", "رمز الدخول",
                   f"اكتب هذا الرمز لإكمال الدخول إلى لوحة {s.cfg.store_name}. صالح 10 دقائق.",
                   box=code, big=True, rows=[("الوقت", moment), ("عنوان IP", address)],
                   outro="إن لم تكن أنت من يحاول الدخول فكلمة المرور معروفة لغيرك: غيّر OWNER_PASSWORD في Render الآن.")


def trial_message(s):
    return message(s, f"رسالة تجريبية من {s.cfg.store_name}", "الإيميل يعمل",
                   f"وصلتك هذه الرسالة من لوحة {s.cfg.store_name}، فإعدادات الإيميل صحيحة.",
                   rows=[("المرسل", s.cfg.smtp_email), ("الخادم", f"{s.cfg.smtp_server}:{s.cfg.smtp_port}")])
