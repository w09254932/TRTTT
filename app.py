"""TRTTT - private payment-invoice desk on top of EdfaPay, stored in Firestore."""
import logging
import secrets
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import quote

from flask import Flask, render_template, request
from markupsafe import Markup
from werkzeug.exceptions import HTTPException

import phones
from config import Config
from declines import explain
from db import KSA, Store, make_client
from edfapay import EdfaPay
from edfapay_legacy import EdfaPayLegacy
from mailer import Mailer, hint, mask
from security import Vault, csrf_token, harden

STATUS_AR = {"pending": "بانتظار الدفع", "paid": "مدفوعة", "cancelled": "ملغاة",
             "expired": "منتهية", "refunded": "مستردة"}
WORDS_AR = {"Purchase": "عملية دفع", "Authorization": "حجز مبلغ", "Capture": "تحصيل مبلغ محجوز", "Refund": "استرداد",
            "Void": "إلغاء حجز", "Approved": "مقبولة", "Declined": "مرفوضة", "Pending": "قيد المعالجة",
            "Redirect": "بانتظار تحقق العميل", "amount_mismatch": "المبلغ لا يطابق الفاتورة فلم تُحتسب",
            "already_paid": "الفاتورة مدفوعة من قبل", "refund_without_payment": "استرداد لفاتورة غير مدفوعة"}
PHONE_CODES = Markup(phones.CODES_JSON)   # static data, built from the country table only
ERRORS_AR = {400: "الطلب غير صحيح.", 404: "الصفحة غير موجودة.", 405: "الطلب غير مسموح.",
             413: "البيانات المرسلة أكبر من المسموح.", 429: "طلبات كثيرة. انتظر دقيقة ثم حاول مرة أخرى."}


def create_app(cfg=None, client=None):
    cfg = cfg or Config()
    problems = cfg.problems()
    secure = not cfg.insecure_dev
    app = Flask(__name__)
    app.logger.setLevel(logging.INFO)
    app.config.update(
        SECRET_KEY=cfg.secret_key or secrets.token_hex(32),
        SESSION_COOKIE_NAME="__Host-trs" if secure else "trs",
        SESSION_COOKIE_SECURE=secure,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),   # the owner's own session ends sooner (security.py)
        MAX_CONTENT_LENGTH=64 * 1024,
    )

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/robots.txt")
    def robots():
        return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}

    @app.after_request
    def headers(response):
        return harden(response, secure)

    support = phones.chat_number(cfg.whatsapp)
    if cfg.whatsapp and not support:
        app.logger.warning("WHATSAPP_NUMBER is not a phone number, so the WhatsApp button is hidden")

    def whatsapp(text=""):
        """A wa.me link to the shop's WhatsApp with the message already written, or "" when none is set."""
        if not support:
            return ""
        return f"https://wa.me/{support}" + (f"?text={quote(text)}" if text else "")

    @app.context_processor
    def shared():
        return {"store_name": cfg.store_name, "csrf_token": csrf_token, "status_ar": STATUS_AR, "words": WORDS_AR,
                "phone_code": phones.code_of, "phone_flag": phones.flag, "phone_label": phones.label,
                "phone_examples": phones.EXAMPLES, "phone_codes": PHONE_CODES, "whatsapp": whatsapp,
                "support_phone": "+" + support if support else "", "support_bad": bool(cfg.whatsapp and not support)}

    @app.template_filter("money")
    def money(value):
        return f"{float(value or 0):,.2f}"

    app.add_template_filter(explain, "why")
    app.add_template_filter(mask, "mask")
    app.add_template_filter(hint, "mailhint")
    app.add_template_filter(phones.pretty, "phone")
    app.add_template_filter(phones.country, "phone_country")
    app.add_template_filter(phones.parts, "phone_parts")

    @app.template_filter("when")
    def when(value, fmt="%Y-%m-%d %H:%M"):
        return value.astimezone(KSA).strftime(fmt) if value else ""

    if problems:
        # Fail closed: nothing works until every required variable is set.
        app.logger.error("configuration incomplete: %s", ", ".join(name for name, _ in problems))

        @app.before_request
        def setup_needed():
            if request.endpoint not in ("healthz", "static", "robots"):
                return render_template("message.html", title="الإعداد غير مكتمل", problems=problems), 503

        return app

    vault = Vault(cfg.encryption_key)
    if cfg.legacy:
        gateway = EdfaPayLegacy(cfg.edfa_base_url, cfg.edfa_merchant_id, cfg.edfa_merchant_password,
                                cfg.edfa_fallback_ip)
    else:
        gateway = EdfaPay(cfg.edfa_base_url, cfg.edfa_api_key, cfg.edfa_webhook_secret)
    from accounts import Accounts, account, signed_in_customer
    from brand import Brand, brand
    from cart import cart, count as cart_count
    from broadcasts import Broadcasts, leave, messages
    from customers import CustomerBook, book
    from legacy_flow import legacy
    from manual import manual_admin, steps_of
    from policies import policies
    from products import Shop, shop, shop_admin
    from twostep import LoginCodes, twostep
    from views_admin import admin
    from views_public import pub
    store = Store(client or make_client(cfg), vault)
    app.extensions["svc"] = SimpleNamespace(cfg=cfg, store=store, gateway=gateway,
                                            book=CustomerBook(store.db, vault), brand=Brand(store.db),
                                            mailer=Mailer(cfg), codes=LoginCodes(store.db, cfg),
                                            shop=Shop(store.db, vault, store), broadcasts=Broadcasts(store.db),
                                            accounts=Accounts(store.db, vault, cfg))
    app.add_template_global(signed_in_customer, "customer_account")
    app.add_template_global(cart_count, "cart_count")
    app.add_template_global(steps_of, "work_steps")
    app.add_template_global(lambda: app.extensions["svc"].brand.get(), "brand_look")
    app.register_blueprint(admin)
    app.register_blueprint(pub)
    app.register_blueprint(book)
    app.register_blueprint(brand)
    app.register_blueprint(twostep)
    app.register_blueprint(shop)
    app.register_blueprint(shop_admin)
    app.register_blueprint(policies)
    app.register_blueprint(messages)
    app.register_blueprint(leave)
    app.register_blueprint(account)
    app.register_blueprint(cart)
    app.register_blueprint(manual_admin)
    if cfg.legacy:
        app.register_blueprint(legacy)

    @app.errorhandler(Exception)
    def failed(exc):
        if isinstance(exc, HTTPException):
            code = exc.code or 500
            custom = exc.description if code == 400 and exc.description != type(exc).description else None
            text = custom or ERRORS_AR.get(code, "تعذر تنفيذ الطلب.")
        else:
            app.logger.exception("unhandled error on %s", request.endpoint)
            code, text = 500, "حدث خطأ غير متوقع. حاول مرة أخرى بعد قليل."
        return render_template("message.html", title=text, problems=None), code

    return app


app = create_app()
