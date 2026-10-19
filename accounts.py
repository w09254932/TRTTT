"""Customer accounts, optional: buying never needs one. A customer signs in with their email and a six-digit code sent
to it (no password to forget). Their account has three parts: my orders (pay a waiting one, have a paid order's code
sent to their email again; the code is never shown on the site), my details (name, mobile and photo, filled in on the
pay page for them) and settings (offer emails, signing out on every device, deleting the account).
An account is a keyed fingerprint of the email; the email, name and mobile are kept encrypted, the photo apart."""
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlparse

from flask import Blueprint, Response, abort, current_app, flash, has_request_context, redirect, render_template, \
    request, session, url_for
from google.cloud import firestore

from brand import image_type
from db import day_key, now
from mailer import MailError, mask
from notices import deliver, message
from phones import clean_phone, parts
from security import check_csrf, csrf_token, too_many
from validate import clean_email, clean_name, western

account = Blueprint("account", __name__)
CODE_TTL, STEP_TTL, RESEND_AFTER, MAX_TRIES, MAX_SENDS, DAILY_CODES = 600, 900, 60, 5, 8, 300
SIGNED_IN_FOR = 30 * 24 * 3600
MAX_PHOTO = 300 * 1024
WRONG = "الرمز غير صحيح."
EXPIRED = "انتهت صلاحية الرمز. اطلب رمزاً جديداً."
NO_PHOTO = "الصورة يجب أن تكون PNG أو JPG أو WebP بحجم أقل من 300 كيلوبايت."
SHOW = {"": "الكل", "paid": "مدفوعة", "pending": "بانتظار الدفع"}
EDIT_FOR = 600       # seconds a code from the email unlocks the saved name and mobile


def account_id(vault, email):
    return vault.index("email:" + email)


class Accounts:
    """The accounts and their sign-in codes. A code is kept only as a keyed hash, with its expiry, tries and
    resend timer, bound to the browser that asked for it."""

    def __init__(self, client, vault, cfg):
        self.db, self.vault = client, vault
        self.col = client.collection("accounts")
        self.photos = client.collection("account_photos")
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
        person = self.vault.open(data.get("enc", ""), "account:" + aid)
        if not person.get("email"):
            return None
        return {"id": aid, "email": person["email"], "name": person.get("name", ""), "phone": person.get("phone", ""),
                "photo": data.get("photo", 0), "epoch": data.get("epoch", 0), "created_at": data.get("created_at")}

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

    def save_details(self, me, name, phone):
        sealed = self.vault.seal({"email": me["email"], "name": name, "phone": phone}, "account:" + me["id"])
        self.col.document(me["id"]).update({"enc": sealed})

    def set_photo(self, aid, data):
        """A new photo, or none (data empty). The version in its address changes so browsers show the new one."""
        if data:
            self.photos.document(aid).set({"img": data, "type": image_type(data)})
        else:
            self.photos.document(aid).delete()
        version = int(time.time()) if data else 0
        self.col.document(aid).update({"photo": version})

    def photo(self, aid):
        data = self.photos.document(aid).get().to_dict() or {}
        img = bytes(data.get("img") or b"")
        return (img, data.get("type")) if img and image_type(img) else (b"", None)

    def sign_out_everywhere(self, aid):
        self.col.document(aid).update({"epoch": firestore.Increment(1)})

    def delete(self, aid):
        self.photos.document(aid).delete()
        self.codes.document(aid).delete()
        self.col.document(aid).delete()

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


def _sign_out():
    for key in ("cust", "ct", "ce", "ac"):
        session.pop(key, None)


def signed_in_customer():
    """The signed-in customer's account, or None. The pages' top bar asks for it."""
    if not has_request_context() or not session.get("cust"):
        return None
    if "trttt.me" not in request.environ:
        me = None
        if time.time() - session.get("ct", 0) <= SIGNED_IN_FOR:
            me = svc().accounts.get(session["cust"])
            if me and me["epoch"] != session.get("ce", 0):  # signed out on every device since
                me = None
        if not me:
            _sign_out()
        request.environ["trttt.me"] = me
    return request.environ["trttt.me"]


def details_for_payment(me):
    """What the pay page fills in for a signed-in customer."""
    form = {"email": me["email"], "name": me["name"]}
    if me["phone"]:
        form["cc"], form["phone"] = parts(me["phone"])
    return form


def _safe_next(value):
    """Only a path on this site, never another site."""
    value = (value or "").strip()
    parts_ = urlparse(value)
    if not value.startswith("/") or value.startswith("//") or parts_.scheme or parts_.netloc or "\\" in value:
        return ""
    return value[:300]


@account.before_request
def limit():
    if too_many("account", 60):
        abort(429)
    if request.endpoint == "account.upload_photo":
        request.max_content_length = MAX_PHOTO + 64 * 1024
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
                me = s.accounts.get(aid)
                csrf = session.get("csrf") or csrf_token()
                session.pop("ac", None)
                session.permanent = True
                session.update(cust=aid, ct=int(time.time()), ce=me["epoch"], csrf=csrf)
                if not me["name"] and not step.get("next"):
                    flash("أهلاً بك. أكمل اسمك ورقم جوالك ليُكتبا عنك في كل طلب.")
                    return redirect(url_for(".details"), 303)
                flash(f"أهلاً {me['name'].split()[0]}، تم تسجيل الدخول." if me["name"] else "تم تسجيل الدخول.")
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
    _sign_out()
    flash("تم تسجيل الخروج.")
    return redirect(url_for("shop.store"), 303)


# ---------------------------------------------------------------- the account's pages
def orders_of(s, me):
    """The account's invoices, newest first: paid with its email, or of a customer saved with it."""
    rows = {inv["id"]: inv for inv in s.store.for_buyer(me["id"])}
    for person in _people(s, me):
        for inv in s.store.for_customer(person["id"]):
            if not inv.get("buyer") or inv["buyer"] == me["id"]:   # an order paid with another email is not theirs
                rows.setdefault(inv["id"], inv)
    return sorted(rows.values(), key=lambda inv: inv["created_at"], reverse=True)


def _people(s, me):
    """The customer book's entries saved with the account's email."""
    return [p for p in s.book.everyone() if p["email"] and p["email"].lower() == me["email"]]


def can_resend(inv, me):
    """The code of a paid order goes again only to the email it was sent to, and that email is the account's."""
    state = (inv.get("delivery") or {}).get("state")
    to_them = inv["deliver_email"].lower() == me["email"] or (inv.get("gift") and inv.get("buyer") == me["id"])
    return bool(inv["status"] == "paid" and inv["has_goods"] and to_them
                and (state == "sent" or (inv.get("product") and state == "failed")))


def _me():
    me = signed_in_customer()
    if not me:
        abort(redirect(url_for(".login", next=request.path)))
    return me


def _render(page, me, rows=None, code=200, **extra):
    rows = orders_of(svc(), me) if rows is None else rows
    paid = [inv for inv in rows if inv["status"] == "paid"]
    stats = {"orders": len(rows), "paid": len(paid), "spent": sum(inv["amount"] for inv in paid),
             "waiting": sum(1 for inv in rows if inv["status"] == "pending" and not inv["overdue"])}
    return render_template(f"account_{page}.html", me=me, page=page, stats=stats, **extra), code


@account.get("/account")
def orders():
    me = _me()
    rows, show = orders_of(svc(), me), request.args.get("show", "")
    show = show if show in SHOW else ""
    listed = [inv for inv in rows if not show or (inv["status"] == show and not (show == "pending" and inv["overdue"]))]
    return _render("orders", me, rows, listed=listed, show=show, shows=SHOW, can_resend=can_resend)


@account.post("/account/orders/<inv_id>/resend")
def resend(inv_id):
    s, me = svc(), _me()
    inv = next((inv for inv in orders_of(s, me) if inv["id"] == inv_id), None)
    if not inv or not can_resend(inv, me):
        abort(404)
    if too_many("account_resend", 3, per=600):
        flash("طلبت الإرسال كثيراً. انتظر دقائق ثم حاول مرة أخرى.", "error")
        return redirect(url_for(".orders"), 303)
    state, _ = deliver(s, inv_id, again=True)
    if state == "sent" and inv.get("gift"):
        flash(f"أرسلنا الهدية مرة أخرى إلى بريد صديقك {mask(inv['deliver_email'])}.")
    elif state == "sent":
        flash(f"أرسلنا طلبك مرة أخرى إلى {me['email']}. إن لم تجده فتفقّد البريد غير المرغوب فيه.")
    else:
        flash("تعذر الإرسال الآن. حاول بعد قليل أو تواصل معنا.", "error")
    return redirect(url_for(".orders"), 303)


def _unlocked(me):
    """A code from the email was entered in the last minutes: the saved name and mobile may change."""
    return session.get("edit", {}).get("a") == me["id"] and session["edit"].get("until", 0) > time.time()


@account.route("/account/details", methods=["GET", "POST"])
def details():
    """The name and mobile are written once. A saved one changes only after a code sent to the account's email."""
    s, me = svc(), _me()
    open_ = _unlocked(me)
    locked = {"name": bool(me["name"]) and not open_, "phone": bool(me["phone"]) and not open_}
    nxt = _safe_next(request.values.get("next"))
    form, error = None, None
    if request.method == "POST":
        form = request.form
        name, error = (me["name"], None) if locked["name"] else clean_name(form.get("name", ""))
        phone = me["phone"] if locked["phone"] else ""
        if not error and not locked["phone"] and form.get("phone", "").strip():
            phone, error = clean_phone(form.get("phone", ""), form.get("cc"))
        if not error and not phone:
            error = "اكتب رقم جوالك."
        if not error:
            s.accounts.save_details(me, name, phone)
            session.pop("edit", None)
            flash("تم حفظ بياناتك، وستُكتب في طلباتك. لا يمكن تغييرها لاحقاً إلا برمز يصل إلى بريدك.")
            return redirect(nxt or url_for(".details"), 303)
    if form is None:
        form = {"name": me["name"]}
        if me["phone"]:
            form["cc"], form["phone"] = parts(me["phone"])
    asking = bool(session.get("edit", {}).get("n")) and session["edit"].get("a") == me["id"] and not open_
    return _render("details", me, form=form, error=error, locked=locked, unlocked=open_, asking=asking, next=nxt,
                   code=400 if error else 200)


def _edit_mail(s, code):
    return message(s, f"رمز تعديل بياناتك في {s.cfg.store_name}: {code}", "رمز تعديل البيانات",
                   "اكتب هذا الرمز في صفحة «بياناتي» لتعديل اسمك أو رقم جوالك. صالح 10 دقائق.",
                   box=code, big=True, outro="إن لم تطلب تعديل بياناتك فتجاهل هذه الرسالة، ولن يتغير شيء.")


@account.post("/account/details/unlock")
def unlock():
    """Sends a code to the account's email to change the saved name or mobile."""
    s, me = svc(), _me()
    if too_many("account_code", 6, per=600):
        flash("طلبات كثيرة. انتظر دقائق ثم حاول مرة أخرى.", "error")
        return redirect(url_for(".details"), 303)
    nonce = secrets.token_urlsafe(16)
    code, wait = s.accounts.issue(me["id"], nonce)
    if code is None:
        flash(f"أرسلنا رمزاً قبل قليل. اطلب رمزاً جديداً بعد {wait} ثانية." if wait > 0 else "طلبت رموزاً كثيرة. حاول بعد ساعة.",
              "error")
        return redirect(url_for(".details"), 303)
    try:
        s.mailer.send(me["email"], *_edit_mail(s, code))
    except MailError as exc:
        s.accounts.unsent(me["id"])
        current_app.logger.warning("account edit code not sent: %s", exc.kind)
        flash("تعذر إرسال الرمز إلى بريدك. حاول بعد قليل.", "error")
        return redirect(url_for(".details"), 303)
    session["edit"] = {"a": me["id"], "n": nonce}
    flash(f"أرسلنا رمزاً إلى {me['email']}. اكتبه هنا لتعديل بياناتك.")
    return redirect(url_for(".details"), 303)


@account.post("/account/details/verify")
def verify():
    s, me = svc(), _me()
    step = session.get("edit") or {}
    typed = western(request.form.get("code", "")).strip().replace(" ", "")
    if step.get("a") != me["id"] or not step.get("n"):
        return redirect(url_for(".details"), 303)
    if too_many("account_check", 20, per=600):
        flash("محاولات كثيرة. انتظر دقائق.", "error")
        return redirect(url_for(".details"), 303)
    outcome = s.accounts.check(me["id"], step["n"], typed) if typed.isdigit() and len(typed) == 6 else "wrong"
    if outcome == "ok":
        session["edit"] = {"a": me["id"], "until": int(time.time()) + EDIT_FOR}
        flash("يمكنك الآن تعديل اسمك ورقم جوالك خلال 10 دقائق.")
    else:
        if outcome == "expired":
            session.pop("edit", None)
        flash(WRONG if outcome == "wrong" else EXPIRED, "error")
    return redirect(url_for(".details"), 303)


@account.post("/account/photo")
def upload_photo():
    s, me = svc(), _me()
    upload = request.files.get("photo")
    data = upload.read(MAX_PHOTO + 1) if upload and upload.filename else b""
    if not data or len(data) > MAX_PHOTO or not image_type(data):
        flash(NO_PHOTO, "error")
    else:
        s.accounts.set_photo(me["id"], data)
        flash("تم تحديث صورتك.")
    return redirect(url_for(".details"), 303)


@account.post("/account/photo/remove")
def remove_photo():
    s, me = svc(), _me()
    s.accounts.set_photo(me["id"], b"")
    flash("تم حذف صورتك.")
    return redirect(url_for(".details"), 303)


@account.get("/account/photo")
def photo():
    """The signed-in customer's own photo only."""
    me = signed_in_customer()
    img, kind = svc().accounts.photo(me["id"]) if me else (b"", None)
    if not img:
        abort(404)
    return Response(img, mimetype=kind, headers={"Cache-Control": "private, max-age=86400"})


@account.get("/account/settings")
def settings():
    s, me = svc(), _me()
    people = _people(s, me)
    return _render("settings", me, offers=not any(p["optout"] for p in people), saved=bool(people))


@account.post("/account/settings/offers")
def offers():
    s, me = svc(), _me()
    on = request.form.get("offers") == "1"
    for person in _people(s, me):
        (s.book.start_mail if on else s.book.stop_mail)(person["id"])
    flash("ستصلك عروض المتجر وأخباره على بريدك." if on else "لن تصلك عروض المتجر وأخباره. رسائل طلباتك تصلك كالمعتاد.")
    return redirect(url_for(".settings"), 303)


@account.post("/account/settings/everywhere")
def everywhere():
    s, me = svc(), _me()
    s.accounts.sign_out_everywhere(me["id"])
    _sign_out()
    flash("تم تسجيل الخروج من كل الأجهزة. سجّل الدخول من جديد متى شئت.")
    return redirect(url_for(".login"), 303)


@account.post("/account/delete")
def delete():
    s, me = svc(), _me()
    s.accounts.delete(me["id"])
    _sign_out()
    current_app.logger.info("account %s deleted by its owner", me["id"][:8])
    flash("تم حذف حسابك. طلباتك السابقة باقية في سجلات المتجر كما تنص سياسة الخصوصية.")
    return redirect(url_for("shop.store"), 303)
