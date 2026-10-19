"""Customer accounts, optional: buying never needs one. A customer signs in with their email and a six-digit code sent
to it (no password to forget), then sees their orders, pays a waiting one, and has the code of a paid order sent to
their email again. The code is never shown on the site. An account is only a keyed fingerprint of the email plus the
email itself, encrypted; orders are found by the email they were paid with, or through the customer book."""
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, has_request_context, redirect, render_template, request, \
    session, url_for
from google.cloud import firestore

from db import day_key, now
from mailer import MailError
from notices import deliver, message
from security import check_csrf, csrf_token, too_many
from validate import clean_email, western

account = Blueprint("account", __name__)
CODE_TTL, STEP_TTL, RESEND_AFTER, MAX_TRIES, MAX_SENDS, DAILY_CODES = 600, 900, 60, 5, 8, 300
SIGNED_IN_FOR = 30 * 24 * 3600
WRONG = "الرمز غير صحيح."
EXPIRED = "انتهت صلاحية الرمز. اطلب رمزاً جديداً."


def account_id(vault, email):
    return vault.index("email:" + email)


class Accounts:
    """The accounts and their sign-in codes. A code is kept only as a keyed hash, with its expiry, tries and
    resend timer, bound to the browser that asked for it."""

    def __init__(self, client, vault, cfg):
        self.db, self.vault = client, vault
        self.col = client.collection("accounts")
        self.codes = client.collection("account_codes")
        self.sends = client.collection("meta").document("account_code_sends")
        self._key = hmac.new(cfg.secret_key.encode(), b"trttt-account-code", hashlib.sha256).digest()

    def _mac(self, nonce, code):
        return hmac.new(self._key, f"{nonce}:{code}".encode(), hashlib.sha256).hexdigest()

    def get(self, aid):
        snap = self.col.document(aid).get() if aid else None
        if not snap or not snap.exists:
            return None
        data = snap.to_dict() or {}
        email = self.vault.open(data.get("enc", ""), "account:" + aid).get("email", "")
        return {"id": aid, "email": email, "created_at": data.get("created_at")} if email else None

    def open_for(self, email):
        """The account of this email, made on its first sign-in."""
        aid, moment = account_id(self.vault, email), now()
        ref = self.col.document(aid)
        if ref.get().exists:
            ref.update({"last_at": moment})
        else:
            ref.set({"enc": self.vault.seal({"email": email}, "account:" + aid), "created_at": moment,
                     "last_at": moment})
        return aid

    def issue(self, aid, nonce):
        """A new code for this browser. Returns (code, wait): wait > 0 is the seconds before another may be sent,
        -1 too many codes for this email this hour, -2 too many codes today for the whole store."""
        moment, ref = time.time(), self.codes.document(aid)
        old = ref.get().to_dict() or {}
        if moment - old.get("sent", 0) < RESEND_AFTER:
            return None, int(RESEND_AFTER - (moment - old["sent"])) + 1
        fresh = moment - old.get("first", 0) > 3600
        if not fresh and old.get("sends", 0) >= MAX_SENDS:
            return None, -1
        today = self.sends.get().to_dict() or {}
        count = today.get("n", 0) if today.get("day") == day_key() else 0
        if count >= DAILY_CODES:  # keeps the mailbox's sending allowance for the orders
            return None, -2
        self.sends.set({"day": day_key(), "n": count + 1})
        code = f"{secrets.randbelow(10 ** 6):06d}"
        ref.set({"mac": self._mac(nonce, code), "nonce": nonce, "exp": moment + CODE_TTL, "tries": 0, "sent": moment,
                 "first": moment if fresh else old["first"], "sends": 1 if fresh else old.get("sends", 0) + 1})
        return code, 0

    def unsent(self, aid):
        """The email did not go out: allow asking for another code straight away."""
        self.codes.document(aid).set({"sent": 0}, merge=True)

    def check(self, aid, nonce, code):
        """'ok' (the code is used up), 'wrong', or 'expired' (also after too many tries or for another browser)."""
        moment, ref = time.time(), self.codes.document(aid)

        def work(tx):
            data = next(iter(tx.get(ref))).to_dict() or {}
            tries = data.get("tries", 0)
            if (not hmac.compare_digest(str(data.get("nonce", "")), nonce) or data.get("exp", 0) < moment
                    or tries >= MAX_TRIES):
                return "expired"
            if hmac.compare_digest(str(data.get("mac", "")), self._mac(nonce, code)):
                tx.delete(ref)
                return "ok"
            tx.update(ref, {"tries": tries + 1})
            return "wrong" if tries + 1 < MAX_TRIES else "expired"

        return firestore.transactional(work)(self.db.transaction())


def svc():
    return current_app.extensions["svc"]


def signed_in_customer():
    """The signed-in customer's account, or None. The pages' top bar asks for it."""
    if not has_request_context() or not session.get("cust"):
        return None
    if time.time() - session.get("ct", 0) > SIGNED_IN_FOR:
        session.pop("cust", None)
        return None
    if "trttt.me" not in request.environ:
        request.environ["trttt.me"] = svc().accounts.get(session["cust"])
    return request.environ["trttt.me"]


def _safe_next(value):
    """Only a path on this site, never another site."""
    value = (value or "").strip()
    parts = urlparse(value)
    if not value.startswith("/") or value.startswith("//") or parts.scheme or parts.netloc or "\\" in value:
        return ""
    return value[:300]


@account.before_request
def limit():
    if too_many("account", 60):
        abort(429)
    if request.method == "POST":
        check_csrf()


def _code_mail(s, code):
    return message(s, f"رمز الدخول إلى {s.cfg.store_name}: {code}", "رمز الدخول",
                   f"اكتب هذا الرمز لتسجيل الدخول إلى حسابك في {s.cfg.store_name}. صالح 10 دقائق.",
                   box=code, big=True, outro="إن لم تطلب هذا الرمز فتجاهل هذه الرسالة، ولن يدخل أحد إلى حسابك دونه.")


def _send_code(s, aid, email):
    """Sends a code. Returns (ready, why): ready when a code is on its way (now or a minute ago)."""
    step = session.get("ac") or {}
    nonce = step.get("n") if step.get("a") == aid and step.get("n") else secrets.token_urlsafe(16)
    code, wait = s.accounts.issue(aid, nonce)
    if code is None:
        if wait == -1:
            return False, "طلبت رموزاً كثيرة لهذا البريد. حاول بعد ساعة."
        if wait == -2:
            return False, "تعذر إرسال الرمز الآن. حاول لاحقاً."
        if step.get("a") != aid:  # a code went to this email from another browser a moment ago
            return False, f"أُرسل رمز إلى هذا البريد قبل قليل. حاول بعد {wait} ثانية."
        session["ac"] = {**step, "t": int(time.time())}
        return True, f"أرسلنا رمزاً قبل قليل. يمكنك طلب رمز جديد بعد {wait} ثانية."
    try:
        s.mailer.send(email, *_code_mail(s, code))
    except MailError as exc:
        s.accounts.unsent(aid)
        current_app.logger.warning("account code not sent: %s", exc.kind)
        return False, "تعذر إرسال الرمز إلى بريدك. تأكد من البريد وحاول مرة أخرى."
    session["ac"] = {"a": aid, "e": email, "n": nonce, "t": int(time.time()), "next": step.get("next", "")}
    return True, ""


@account.route("/account/login", methods=["GET", "POST"])
def login():
    s = svc()
    if signed_in_customer():
        return redirect(_safe_next(request.args.get("next")) or url_for(".orders"))
    if request.method == "GET":
        return render_template("account_login.html", error=None, email="", next=_safe_next(request.args.get("next")))
    email, error = clean_email(western(request.form.get("email", "")))
    if error:
        error = "اكتب بريداً إلكترونياً صحيحاً."
    elif not s.mailer.ready:
        error = "تسجيل الدخول غير متاح الآن. يمكنك الشراء دون حساب."
    elif too_many("account_code", 6, per=600):
        error = "طلبات كثيرة. انتظر دقائق ثم حاول مرة أخرى."
    else:
        session["ac"] = {**(session.get("ac") or {}), "next": _safe_next(request.form.get("next"))}
        ready, error = _send_code(s, account_id(s.store.vault, email), email)
        if ready:
            if error:
                flash(error)
            return redirect(url_for(".code"), 303)
    return render_template("account_login.html", error=error, email=request.form.get("email", "")[:120],
                           next=_safe_next(request.form.get("next"))), 400


def _step():
    step = session.get("ac") or {}
    if not step.get("a") or not step.get("n") or time.time() - step.get("t", 0) > STEP_TTL:
        return None
    return step


@account.route("/account/code", methods=["GET", "POST"])
def code():
    s, step = svc(), _step()
    if not step:
        flash("اكتب بريدك لنرسل لك رمز الدخول.", "error")
        return redirect(url_for(".login"))
    error = None
    if request.method == "POST":
        typed = western(request.form.get("code", "")).strip().replace(" ", "")
        if too_many("account_check", 20, per=600):
            error = "محاولات كثيرة. انتظر دقائق."
        else:
            outcome = s.accounts.check(step["a"], step["n"], typed) if typed.isdigit() and len(typed) == 6 else "wrong"
            if outcome == "ok":
                aid = s.accounts.open_for(step["e"])
                csrf = session.get("csrf") or csrf_token()
                session.pop("ac", None)
                session.permanent = True
                session.update(cust=aid, ct=int(time.time()), csrf=csrf)
                flash("تم تسجيل الدخول.")
                return redirect(step.get("next") or url_for(".orders"), 303)
            error = WRONG if outcome == "wrong" else EXPIRED
    return render_template("account_code.html", error=error, email=step["e"]), 400 if error else 200


@account.post("/account/code/again")
def again():
    s, step = svc(), _step()
    if not step:
        return redirect(url_for(".login"))
    ready, error = _send_code(s, step["a"], step["e"])
    flash(error or "أرسلنا رمزاً جديداً.", "info" if ready else "error")
    return redirect(url_for(".code"), 303)


@account.post("/account/logout")
def logout():
    for key in ("cust", "ct", "ac"):
        session.pop(key, None)
    flash("تم تسجيل الخروج.")
    return redirect(url_for("shop.store"), 303)


def orders_of(s, me):
    """The account's invoices, newest first: paid with its email, or of a customer saved with it."""
    rows = {inv["id"]: inv for inv in s.store.for_buyer(me["id"])}
    for person in s.book.everyone():
        if person["email"] and person["email"].lower() == me["email"]:
            for inv in s.store.for_customer(person["id"]):
                rows.setdefault(inv["id"], inv)
    return sorted(rows.values(), key=lambda inv: inv["created_at"], reverse=True)


def can_resend(inv, me):
    """The code of a paid order goes again only to the email it was sent to, and that email is the account's."""
    state = (inv.get("delivery") or {}).get("state")
    return bool(inv["status"] == "paid" and inv["has_goods"] and inv["deliver_email"].lower() == me["email"]
                and (state == "sent" or (inv.get("product") and state == "failed")))


@account.get("/account")
def orders():
    me = signed_in_customer()
    if not me:
        return redirect(url_for(".login", next=url_for(".orders")))
    return render_template("account.html", me=me, rows=orders_of(svc(), me), can_resend=can_resend)


@account.post("/account/orders/<inv_id>/resend")
def resend(inv_id):
    s, me = svc(), signed_in_customer()
    if not me:
        return redirect(url_for(".login"))
    inv = next((inv for inv in orders_of(s, me) if inv["id"] == inv_id), None)
    if not inv or not can_resend(inv, me):
        abort(404)
    if too_many("account_resend", 3, per=600):
        flash("طلبت الإرسال كثيراً. انتظر دقائق ثم حاول مرة أخرى.", "error")
        return redirect(url_for(".orders"), 303)
    state, _ = deliver(s, inv_id, again=True)
    if state == "sent":
        flash(f"أرسلنا طلبك مرة أخرى إلى {me['email']}. إن لم تجده فتفقّد البريد غير المرغوب فيه.")
    else:
        flash("تعذر الإرسال الآن. حاول بعد قليل أو تواصل معنا.", "error")
    return redirect(url_for(".orders"), 303)
