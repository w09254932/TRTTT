"""Second sign-in step: after the number and password, a six-digit code is emailed to OWNER_EMAIL.
The owner turns it on from Settings by entering a code, which proves the mailbox works. If email ever stops
working, removing OWNER_EMAIL in Render turns the step off, so access to Render is the way back in."""
import hashlib
import hmac
import math
import re
import secrets
import time

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from google.cloud import firestore

from db import day_key, now
from mailer import MailError, mask
from notices import code_message, trial_message
from security import check_csrf, check_owner, client_ip, ip_key, signed_in, too_many
from validate import western
from views_admin import finish_login, guard

twostep = Blueprint("twostep", __name__)
CODE_TTL, STEP_TTL, RESEND_AFTER, MAX_TRIES, MAX_SENDS, DAILY_CODES = 600, 900, 60, 5, 5, 40
WRONG = "الرمز غير صحيح."
EXPIRED = "انتهت صلاحية الرمز. اطلب رمزاً جديداً."
ESCAPE = " إن تعطل الإيميل فاحذف OWNER_EMAIL من Render مؤقتاً لتدخل بكلمة المرور وحدها."


class LoginCodes:
    """One code at a time, kept in Firestore only as a keyed hash, with its expiry, tries and resend timer.
    The on/off switch remembers (as a keyed hash) which address was proven to receive the codes."""

    def __init__(self, client, cfg):
        self.db = client
        self.ref = client.collection("meta").document("login_code")
        self.flag = client.collection("meta").document("two_step")
        self.sends = client.collection("meta").document("login_code_sends")
        self._key = hmac.new(cfg.secret_key.encode(), b"trttt-login-code", hashlib.sha256).digest()
        self._epoch, self._epoch_at = 0, 0.0

    def _mac(self, nonce, code):
        return hmac.new(self._key, f"{nonce}:{code}".encode(), hashlib.sha256).hexdigest()

    def _state(self):
        return self.flag.get().to_dict() or {}

    def enabled(self):
        return bool(self._state().get("on"))

    def enabled_for(self, email):
        """On, and for this very address: a changed OWNER_EMAIL needs to be proven again."""
        state = self._state()
        return bool(state.get("on")) and hmac.compare_digest(str(state.get("for", "")), self._mac("email", email))

    def set_enabled(self, on, email=""):
        """Turning it on also starts a new session epoch, which signs out every session made before."""
        epoch = int(self._state().get("epoch", 0)) + (1 if on else 0)
        self.flag.set({"on": on, "for": self._mac("email", email) if on else "", "epoch": epoch, "at": now()})
        self._epoch, self._epoch_at = epoch, time.time()
        return epoch

    def epoch(self, fresh=False):
        """Kept for 30 seconds per worker, so checking it costs almost nothing."""
        if fresh or time.time() - self._epoch_at > 30:
            self._epoch, self._epoch_at = int(self._state().get("epoch", 0)), time.time()
        return self._epoch

    def issue(self, nonce, purpose):
        """A new code for this browser, replacing any earlier one. Returns (code, wait):
        wait > 0 is the seconds left before another code may be sent, -1 means too many codes were sent."""
        moment, old = time.time(), self.ref.get().to_dict() or {}
        same = old.get("nonce") == nonce and old.get("purpose") == purpose
        if same and moment - old.get("sent", 0) < RESEND_AFTER:
            return None, int(RESEND_AFTER - (moment - old["sent"])) + 1
        if same and old.get("sends", 0) >= MAX_SENDS:
            return None, -1
        today = self.sends.get().to_dict() or {}
        count = today.get("n", 0) if today.get("day") == day_key() else 0
        if count >= DAILY_CODES:  # keeps the mailbox's sending allowance for customers' orders
            return None, -2
        self.sends.set({"day": day_key(), "n": count + 1})
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self.ref.set({"mac": self._mac(nonce, code), "nonce": nonce, "purpose": purpose, "exp": moment + CODE_TTL,
                      "tries": 0, "sent": moment, "sends": old.get("sends", 0) + 1 if same else 1})
        return code, 0

    def unsent(self):
        """The email did not go out: allow asking for another code straight away."""
        self.ref.set({"sent": 0}, merge=True)

    def check(self, nonce, purpose, code):
        """'ok' (the code is used up), 'wrong', or 'expired' (also after too many tries or for another browser)."""
        moment = time.time()

        def work(tx):
            data = next(iter(tx.get(self.ref))).to_dict() or {}
            tries = data.get("tries", 0)
            if (not hmac.compare_digest(str(data.get("nonce", "")), nonce) or data.get("purpose") != purpose
                    or data.get("exp", 0) < moment or tries >= MAX_TRIES):
                return "expired"
            if hmac.compare_digest(str(data.get("mac", "")), self._mac(nonce, code)):
                tx.delete(self.ref)
                return "ok"
            tx.update(self.ref, {"tries": tries + 1})
            return "wrong" if tries + 1 < MAX_TRIES else "expired"

        return firestore.transactional(work)(self.db.transaction())


def svc():
    return current_app.extensions["svc"]


def needed(s):
    """The step applies once it was turned on for the current OWNER_EMAIL, while email works."""
    return bool(s.mailer.ready and s.cfg.owner_email and s.codes.enabled_for(s.cfg.owner_email))


def _step(purpose):
    step = session.get("two") or {}
    if step.get("p") != purpose or not step.get("n") or time.time() - step.get("t", 0) > STEP_TTL:
        return None
    return step


def _send(s, nonce, purpose):
    """Emails a new code. Returns None, or what to tell the owner."""
    code, wait = s.codes.issue(nonce, purpose)
    if wait > 0:
        return f"انتظر {wait} ثانية ثم اطلب رمزاً جديداً."
    if wait == -2:
        return "وصلت رموز الدخول اليوم إلى الحد المسموح. حاول غداً." + (ESCAPE if purpose == "login" else "")
    if wait < 0:
        return "طلبت رموزاً كثيرة. ابدأ من جديد."
    try:
        s.mailer.send(s.cfg.owner_email, *code_message(s, code, purpose, client_ip()))
    except MailError as exc:
        s.codes.unsent()
        current_app.logger.warning("sign-in code email failed: %s", exc.kind)
        return f"لم يُرسل الرمز: {exc.hint}" + (ESCAPE if purpose == "login" else "")
    current_app.logger.info("sign-in code emailed")
    return None


def _entered():
    return re.sub(r"[^0-9]", "", western(request.form.get("code", "")))[:12]


def begin(s):
    """Called by the login page after a correct number and password: the code is still needed."""
    session.clear()
    session["two"] = {"n": secrets.token_urlsafe(16), "t": int(time.time()), "p": "login"}
    problem = _send(s, session["two"]["n"], "login")
    if problem:
        flash(problem, "error")
    return redirect(url_for("twostep.code"), 303)


@twostep.before_request
def protect():
    if request.endpoint in ("twostep.code", "twostep.resend"):
        if request.method == "POST":
            check_csrf()
        return None
    return guard()


@twostep.route("/login/code", methods=["GET", "POST"])
def code():
    s = svc()
    if signed_in(s.cfg):
        return redirect(url_for("admin.dashboard"))
    step = _step("login")
    if not step or not needed(s):
        session.pop("two", None)
        flash("انتهت مهلة الدخول. سجّل الدخول من جديد.", "error")
        return redirect(url_for("admin.login"))
    error, status = None, 200
    if request.method == "POST":
        address, entered = ip_key(s.cfg), _entered()
        wait = s.store.lock_left(address, "all")
        if wait or too_many("login", 15):
            error, status = f"محاولات كثيرة. حاول بعد {math.ceil((wait or 60) / 60)} دقيقة.", 429
        elif len(entered) != 6:
            error, status = "اكتب الرمز المكوّن من 6 أرقام.", 400
        else:
            outcome = s.codes.check(step["n"], "login", entered)
            if outcome == "ok":
                finish_login(s, address)
                current_app.logger.info("owner signed in with the email code")
                return redirect(url_for("admin.dashboard"), 303)
            s.store.login_failed(address, 5)
            s.store.login_failed("all", 30)
            current_app.logger.warning("wrong sign-in code")
            error, status = (WRONG if outcome == "wrong" else EXPIRED), 401
    return render_template("code.html", error=error, to=mask(s.cfg.owner_email)), status


@twostep.post("/login/code/resend")
def resend():
    s, step = svc(), _step("login")
    if not step or not needed(s):
        return redirect(url_for(".code"), 303)
    if too_many("login", 15):
        flash("طلبات كثيرة. انتظر دقيقة.", "error")
    else:
        problem = _send(s, step["n"], "login")
        flash(problem or "أرسلنا رمزاً جديداً. استخدم الأحدث فقط.", "error" if problem else "info")
    return redirect(url_for(".code"), 303)


# ---- Settings: email status, test message, turning the step on and off
def status(s):
    return {"ready": s.mailer.ready, "problems": s.cfg.mail_problems(), "sender": s.cfg.smtp_email,
            "server": f"{s.cfg.smtp_server}:{s.cfg.smtp_port}", "owner": s.cfg.owner_email,
            "on": s.codes.enabled(), "active": needed(s), "confirming": bool(_step("enable"))}


def _back():
    return redirect(url_for("brand.settings"), 303)


@twostep.post("/settings/email/test")
def test():
    s = svc()
    if too_many("mailtest", 5):
        flash("طلبات كثيرة. انتظر دقيقة.", "error")
        return _back()
    to = s.cfg.owner_email or s.cfg.smtp_email
    try:
        s.mailer.send(to, *trial_message(s))
    except MailError as exc:
        current_app.logger.warning("test email failed: %s", exc.kind)
        flash(f"لم تُرسل الرسالة: {exc.hint}", "error")
    else:
        flash(f"أُرسلت رسالة تجريبية إلى {to}. إن لم تجدها فتفقّد البريد غير المرغوب فيه.", "info")
    return _back()


@twostep.post("/settings/login-code/start")
def start():
    s = svc()
    if not (s.mailer.ready and s.cfg.owner_email):
        flash("أكمل إعداد الإيميل و OWNER_EMAIL في Render أولاً.", "error")
        return _back()
    session["two"] = {"n": secrets.token_urlsafe(16), "t": int(time.time()), "p": "enable"}
    problem = _send(s, session["two"]["n"], "enable")
    flash(problem or f"أرسلنا رمزاً إلى {s.cfg.owner_email}. اكتبه بالأسفل.", "error" if problem else "info")
    return _back()


@twostep.post("/settings/login-code/confirm")
def confirm():
    s, step = svc(), _step("enable")
    entered = _entered()
    if not step:
        flash("انتهت المهلة. ابدأ التفعيل من جديد.", "error")
    elif len(entered) != 6:
        flash("اكتب الرمز المكوّن من 6 أرقام.", "error")
    else:
        outcome = s.codes.check(step["n"], "enable", entered)
        if outcome == "ok":
            session["e"] = s.codes.set_enabled(True, s.cfg.owner_email)  # older sessions elsewhere end
            session.pop("two", None)
            current_app.logger.info("email sign-in code turned on")
            flash("تم التفعيل. من الآن يُطلب رمز من إيميلك بعد كلمة المرور.", "info")
        else:
            flash(WRONG if outcome == "wrong" else EXPIRED, "error")
            if outcome != "wrong":
                session.pop("two", None)
    return _back()


@twostep.post("/settings/login-code/off")
def off():
    s, address = svc(), ip_key(svc().cfg)
    if s.store.lock_left(address, "all") or too_many("login", 15):
        flash("محاولات كثيرة. حاول لاحقاً.", "error")
    elif not check_owner(s.cfg, s.cfg.owner_number, request.form.get("password", "")):
        s.store.login_failed(address, 5)
        s.store.login_failed("all", 30)
        flash("كلمة المرور غير صحيحة.", "error")
    else:
        s.codes.set_enabled(False)
        current_app.logger.info("email sign-in code turned off")
        flash("تم إيقاف رمز الدخول بالإيميل.", "info")
    return _back()
