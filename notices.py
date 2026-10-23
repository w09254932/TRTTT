"""The emails: the customer's order once EdfaPay has confirmed the payment, a notice to the owner for every
payment, the owner's sign-in code and a test message. Payment emails go out once, from a mail thread."""
import time

from flask import current_app, render_template

from db import KSA, now
from mailer import MailError, mask, one_line

RETRY_WAITS = (5, 20)  # seconds between automatic attempts while the mail server cannot be reached
SENT_TEXT = {"sent": "أُرسل إلى بريد العميل", "failed": "تعذر الإرسال. افتح الفاتورة وأعد الإرسال"}
LATE_TEXT = "لم يُرسل تلقائياً لأن الفاتورة كانت {} قبل الدفع. أرسله من صفحة الفاتورة إن كان ذلك صحيحاً"
CLOSED_AR = {"cancelled": "ملغاة", "expired": "منتهية"}
NO_CODE_TEXT = "لم يبق كود في المخزون لهذا الطلب. أضف أكواداً ثم اضغط «تسليم كود من المخزون» في صفحة الفاتورة"


def message(s, subject, heading, intro="", rows=(), box="", box_label="", big=False, button=None, outro="",
            logo=False, body=(), unsubscribe=""):
    """(subject, text, html) for one email. Both versions are built from the same parts and say the same thing.
    body is a list of paragraphs; unsubscribe, the link that stops the owner's messages, ends the email with it."""
    from brand import tokens
    look = s.brand.get()
    color = look["primary"]
    band = tokens(color, look["background"])
    logo_url = f"{s.cfg.base_url}/brand/logo?v={look['version']}" if logo and look["logo"] else ""
    rows = [(label, one_line(value, 200)) for label, value in rows if value]
    html = render_template("mail.html", subject=subject, store=s.cfg.store_name, heading=heading, intro=intro,
                           rows=rows, box=box, box_label=box_label, big=big, button=button, outro=outro,
                           body=body, unsubscribe=unsubscribe,
                           logo=logo_url, color=color, on_color=band["--on-accent"], band=look["background"],
                           band_text=band["--text"], band_muted=band["--muted"])
    text = [heading, ""]
    if intro:
        text += [intro, ""]
    for part in body:
        text += [part, ""]
    if box:
        text += ([box_label + ":"] if box_label else []) + [box, ""]
    if rows:
        text += [f"{label}: {value}" for label, value in rows] + [""]
    if button:
        text += [f"{button[0]}: {button[1]}", ""]
    if outro:
        text += [outro, ""]
    if unsubscribe:
        text += ["--", f"وصلتك هذه الرسالة لأنك من عملاء {s.cfg.store_name}.", f"لإيقاف هذه الرسائل: {unsubscribe}"]
    else:
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
        inv_id = event["order_id"].partition("x")[0]
        try:
            handed = s.shop.assign(inv_id)  # a shop order gets its code from stock; other invoices are left alone
            if handed != "none":
                current_app.logger.info("shop order %s: code %s", inv_id, handed)
        except Exception as exc:  # the payment stands; the owner can hand out the code from the invoice page
            current_app.logger.error("code not handed out for %s: %s", inv_id, type(exc).__name__)
        try:
            after_payment(s, inv_id)
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
            gift = s.store.gift(inv)
            if gift:
                mail = gift_message(s, inv, title, goods, gift)
            else:
                mail = message(s, f"طلبك من {s.cfg.store_name}: {title}", "تم الدفع، وهذا طلبك",
                               f"شكراً لك. استلمنا دفعتك لفاتورة رقم {inv['id']}.",
                               rows=[("الطلب", title), ("المبلغ", f"{inv['amount']:,.2f} ر.س")], box=goods,
                               box_label="طلبك", logo=True,
                               outro=f"احتفظ بهذه الرسالة. إن واجهت مشكلة في طلبك فتواصل مع {s.cfg.store_name}.")
            error = _send(s, to, mail, waits)
            if gift and not error and tries == 1:
                gift_sent(s, inv, title, to)
    finally:  # whatever happens, the invoice never stays marked as "being sent"
        s.store.finish_delivery(inv_id, tries, "failed" if error else "sent", error)
    if error:
        current_app.logger.warning("order email failed for %s: %s", inv_id, error)
        return "failed", error
    current_app.logger.info("order emailed for %s", inv_id)
    return "sent", ""


def gift_message(s, inv, title, goods, gift):
    """The friend's email: who it is from, their message, and the codes. The price is not shown."""
    sender = gift.get("from") or "صديقك"
    return message(s, f"هدية لك من {sender}: {title}", f"وصلتك هدية من {sender}",
                   f"اختار لك {sender} هدية من {s.cfg.store_name}، وهذه أكوادها.",
                   body=[f"«{gift['note']}»"] if gift.get("note") else (), rows=[("الهدية", title)], box=goods,
                   box_label="هديتك", logo=True,
                   outro=f"احتفظ بهذه الرسالة. إن واجهت مشكلة في الكود فتواصل مع {s.cfg.store_name} واذكر رقم {inv['id']}.")


def gift_sent(s, inv, title, to):
    """Tells the buyer the gift arrived, without its codes."""
    buyer = inv["private"].get("email")
    if not buyer or buyer == to:
        return
    mail = message(s, f"وصلت هديتك: {title}", "وصلت هديتك",
                   f"أرسلنا هديتك إلى {mask(to)}. الأكواد عند صاحب الهدية وحده، ولا تظهر لك حفاظاً على خصوصيتها.",
                   rows=[("الهدية", title), ("المبلغ", f"{inv['amount']:,.2f} ر.س"), ("رقم الفاتورة", inv["id"])],
                   logo=True)
    _send(s, buyer, mail)


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
            late = LATE_TEXT.format(CLOSED_AR.get(before, before)) if before != "pending" and not inv.get("product") else ""
            rows.append(("تسليم الطلب", late or SENT_TEXT.get(delivered, "جارٍ الإرسال")))
        elif inv.get("product"):
            rows.append(("تسليم الطلب", NO_CODE_TEXT))
        short = s.shop.missing(inv) if inv.get("product") else 0
        if short and inv.get("goods"):
            rows.append(("أكواد ناقصة", f"{short}. " + NO_CODE_TEXT))
        lines = inv.get("lines") or ([{"pid": inv["product"], "name": ""}] if inv.get("product") else [])
        handmade = [line for line in lines if line.get("manual")]
        if handmade:
            from manual import open_answers
            typed = open_answers(s.store.vault, inv)
            for line in handmade:
                said = "، ".join(f"{label}: {value}" for label, value in typed.get(line["pid"], []) if value)
                rows.append((f"نفّذ يدوياً: {line['name']} × {line['qty']}", said or "بدون بيانات"))
        for line in lines:
            if line.get("manual"):
                continue
            product = s.shop.get(line["pid"])
            label = "المتبقي من أكواد المنتج" if len(lines) == 1 else f"المتبقي من {line['name']}"
            rows.append((label, str(product["available"]) if product else "المنتج محذوف"))
        heading = "وصلتك دفعة جديدة" if before == "pending" else f"وصلتك دفعة لفاتورة {CLOSED_AR.get(before, before)}"
        mail = message(s, f"دفعة جديدة {amount}: {title}", heading, rows=rows,
                       button=("فتح الفاتورة", f"{s.cfg.base_url}/admin/invoices/{inv['id']}"))
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
