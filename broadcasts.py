"""Messages from the owner to the customers by email: offers, news, a note to one customer. Every customer gets a
separate email with their own name, so nobody sees another customer's address. Sending runs in the background, one
email at a time with a pause between them, and its progress is kept in Firestore: the page shows it, the owner can stop
it, and a run cut short (a deploy restarts the server) is resumed where it stopped. Every email ends with a link that
stops these messages for that customer; the emails of their orders are not affected."""
import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from google.cloud import firestore

from db import now
from mailer import MailError, hint, mask, one_line
from notices import message
from security import too_many
from validate import MAIL_ADDRESS
from views_admin import guard

messages = Blueprint("messages", __name__, url_prefix="/admin")
messages.before_request(guard)
leave = Blueprint("leave", __name__)

PAUSE = 1.5                  # seconds between two emails, gentle on the mailbox's sending limits
STALE = 180                  # a run that has not moved for this long was cut short
LIVE = ("sending", "stopping")  # a runner is working on it; stopping: the owner asked it to stop
STREAK = 5                   # this many refusals in a row: the mailbox reached its daily limit
NEEDS_FIX = ("off", "auth", "connect", "tls")   # the mailbox itself fails: stop instead of failing every customer
SUBJECT_MAX, BODY_MAX = 120, 3000
BIG_LIST = 400               # mailboxes cap the emails a day (Gmail about 500)
BID_RE = re.compile(r"^[0-9a-f]{20}$")
CID_RE = re.compile(r"^[0-9a-f]{32}$")
WHY = {
    "owner": "أوقفته أنت.",
    "cut": "انقطع الإرسال، غالباً بسبب تحديث الموقع أو إعادة تشغيله.",
    "limit": f"رفض خادم البريد {STREAK} رسائل متتالية، وغالباً وصل بريدك إلى حد الإرسال اليومي (في Gmail نحو 500 رسالة "
             "في اليوم). أكمل الإرسال غداً.",
    "failed": "توقف بسبب خطأ غير متوقع. أكمل الإرسال بعد قليل.",
}
_threads = {}


def customers_ar(n):
    return {1: "عميل واحد", 2: "عميلين"}.get(n) or (f"{n} عملاء" if 3 <= n <= 10 else f"{n} عميلاً")


def minutes_ar(n):
    return "أقل من دقيقة" if n <= 1 else "نحو دقيقتين" if n == 2 else f"نحو {n} دقائق" if n <= 10 else f"نحو {n} دقيقة"


def why(kind):
    return WHY.get(kind) or hint(kind)


def paragraphs(body):
    return [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]


def leave_key(cfg, cid):
    return hmac.new(cfg.secret_key.encode(), b"unsubscribe:" + cid.encode(), hashlib.sha256).hexdigest()[:32]


def leave_url(cfg, cid):
    return f"{cfg.base_url}/unsubscribe/{cid}/{leave_key(cfg, cid)}"


def letter(s, run, name, unsubscribe, trial=False):
    """(subject, text, html) of the email one customer gets."""
    first = (name or "").split()[0] if (name or "").strip() else ""
    greeting = (f"مرحباً {first}،" if first else "مرحباً،") if run["greet"] else ""
    button = ("زيارة المتجر", f"{s.cfg.base_url}/") if run["button"] else None
    subject = ("[تجربة] " if trial else "") + run["subject"]
    return message(s, subject, run["subject"], greeting, body=paragraphs(run["body"]), button=button, logo=True,
                   unsubscribe=unsubscribe)


def one_click(url):
    """Headers that let mail apps show their own unsubscribe button (RFC 8058)."""
    return {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


class Broadcasts:
    def __init__(self, client):
        self.db, self.col = client, client.collection("broadcasts")

    def _load(self, snap):
        if not snap.exists:
            return None
        run = dict(snap.to_dict() or {}, id=snap.id)
        if run["state"] in LIVE and run["beat"] < now() - timedelta(seconds=STALE):
            run["state"], run["error"] = "stopped", "owner" if run["state"] == "stopping" else "cut"
        run["left"] = run["total"] - run["done"]
        return run

    def get(self, bid):
        return self._load(self.col.document(bid).get()) if BID_RE.match(bid or "") else None

    def recent(self, limit=10):
        query = self.col.order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit)
        return [self._load(snap) for snap in query.stream()]

    def active(self):
        """The run being sent or stopping now, if any (one at a time)."""
        runs = (self._load(snap) for snap in self.col.where("state", "in", list(LIVE)).stream())
        return next((run for run in runs if run["state"] in LIVE), None)

    def create(self, draft, ids):
        bid, moment = secrets.token_hex(10), now()
        self.col.document(bid).set({**draft, "ids": ids, "total": len(ids), "done": 0, "sent": 0, "failed": 0,
                                    "skipped": 0, "state": "stopped", "error": "", "runner": "",
                                    "created_at": moment, "beat": moment})
        return bid

    def claim(self, bid):
        """Marks the run as sending by a new runner and returns the runner's token, or None when it is already being
        sent or has nothing left. Only the latest runner may send, so a run is never sent twice at once."""
        runner = secrets.token_hex(8)
        ref = self.col.document(bid)

        def work(tx):
            run = self._load(next(iter(tx.get(ref))))
            if not run or run["state"] != "stopped" or run["left"] <= 0:
                return None
            tx.update(ref, {"state": "sending", "runner": runner, "error": "", "beat": now()})
            return runner

        return firestore.transactional(work)(self.db.transaction())

    def request_stop(self, bid):
        """The owner pressed stop: the runner stops after the email it is sending. Returns the run, or None when it
        was not being sent."""
        ref = self.col.document(bid)

        def work(tx):
            run = self._load(next(iter(tx.get(ref))))
            if not run or run["state"] != "sending":
                return None
            tx.update(ref, {"state": "stopping"})
            return run

        return firestore.transactional(work)(self.db.transaction())

    def step(self, bid, done, outcome):
        self.col.document(bid).update({"done": done, outcome: firestore.Increment(1), "beat": now()})

    def halt(self, bid, error, back=0):
        changes = {"state": "stopped", "error": error, "beat": now()}
        if back:  # the refusals just before the stop are sent again on resume
            changes.update({"done": firestore.Increment(-back), "failed": firestore.Increment(-back)})
        self.col.document(bid).update(changes)

    def finish(self, bid):
        self.col.document(bid).update({"state": "done", "error": "", "beat": now(), "finished_at": now()})


def start(app, bid):
    runner = app.extensions["svc"].broadcasts.claim(bid)
    if not runner:
        return False
    thread = threading.Thread(target=_run, args=(app, bid, runner), name=f"broadcast-{bid}", daemon=True)
    _threads[bid] = thread
    thread.start()
    return True


def wait(timeout=30):
    """Waits for the sending threads (used by tests)."""
    for thread in list(_threads.values()):
        thread.join(timeout)


def _run(app, bid, runner):
    with app.app_context():
        s = app.extensions["svc"]
        try:
            _send_all(s, bid, runner)
        except Exception as exc:
            app.logger.error("broadcast %s stopped: %s", bid, type(exc).__name__)
            run = s.broadcasts.get(bid)
            if run and run["runner"] == runner and run["state"] in LIVE:
                s.broadcasts.halt(bid, "owner" if run["state"] == "stopping" else "failed")
        finally:
            _threads.pop(bid, None)


def _send_all(s, bid, runner):
    streak = 0
    while True:
        run = s.broadcasts.get(bid)
        if not run or run["runner"] != runner or run["state"] not in LIVE:
            return  # another runner took over, or the run was cut short and resumed
        if run["state"] == "stopping":
            s.broadcasts.halt(bid, "owner")
            current_app.logger.info("broadcast %s stopped by the owner after %s", bid, run["sent"])
            return
        if run["left"] <= 0:
            s.broadcasts.finish(bid)
            current_app.logger.info("broadcast %s done: %s sent, %s failed", bid, run["sent"], run["failed"])
            return
        cid = run["ids"][run["done"]]
        who = s.book.get(cid)
        if not who or not who["email"] or who["optout"]:
            s.broadcasts.step(bid, run["done"] + 1, "skipped")
            continue
        url = leave_url(s.cfg, cid)
        try:
            s.mailer.send(who["email"], *letter(s, run, who["name"], url), headers=one_click(url))
            outcome, streak = "sent", 0
        except MailError as exc:
            if exc.kind in NEEDS_FIX:
                s.broadcasts.halt(bid, exc.kind)
                current_app.logger.warning("broadcast %s stopped: %s", bid, exc.kind)
                return
            if exc.kind == "unsure":   # it may well have arrived: never sent twice
                outcome, streak = "sent", 0
            elif exc.kind == "address":
                outcome = "skipped"
            else:
                streak += 1
                if streak >= STREAK:
                    s.broadcasts.halt(bid, "limit", back=streak - 1)
                    current_app.logger.warning("broadcast %s stopped: %s refusals in a row", bid, streak)
                    return
                outcome = "failed"
        s.broadcasts.step(bid, run["done"] + 1, outcome)
        time.sleep(PAUSE)


# ---------------------------------------------------------------- the owner's page
def svc():
    return current_app.extensions["svc"]


def _audience(s):
    """(customers who can get messages, how many stopped them, how many have no email)."""
    people = s.book.everyone()
    ready = [p for p in people if p["email"] and not p["optout"]]
    return ready, sum(1 for p in people if p["email"] and p["optout"]), sum(1 for p in people if not p["email"])


def _page(form=None, error=None, code=200, note=None):
    s = svc()
    ready, stopped, no_email = _audience(s)
    runs, active = s.broadcasts.recent(10), s.broadcasts.active()
    eta = minutes_ar(math.ceil(active["left"] * (PAUSE + 2) / 60)) if active else ""   # about 2 s to hand over one
    chosen = request.args.get("to", "")
    form = form or {"greet": "1", "button": "1", "to": chosen if any(p["id"] == chosen for p in ready) else "all"}
    return render_template("messages.html", people=ready, stopped=stopped, no_email=no_email, runs=runs,
                           active=active, eta=eta, form=form, error=error, note=note, mail_ready=s.mailer.ready,
                           owner_email=s.cfg.owner_email, big=len(ready) > BIG_LIST, why=why, many=customers_ar,
                           subject_max=SUBJECT_MAX, body_max=BODY_MAX), code


def _draft(form):
    """(draft, recipients, error) from the form."""
    subject = one_line(form.get("subject", ""), 1000)
    body = form.get("body", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not subject:
        return None, [], "اكتب عنوان الرسالة."
    if len(subject) > SUBJECT_MAX:
        return None, [], f"العنوان أطول من {SUBJECT_MAX} حرفاً."
    if not body:
        return None, [], "اكتب نص الرسالة."
    if len(body) > BODY_MAX:
        return None, [], f"نص الرسالة أطول من {BODY_MAX} حرف."
    ready = _audience(svc())[0]
    to = form.get("to", "all")
    people = ready if to == "all" else [p for p in ready if p["id"] == to]
    if not people:
        return None, [], "لا يوجد عميل يمكن أن تصله الرسالة. اختر المستلمين مرة أخرى."
    draft = {"subject": subject, "body": body, "greet": form.get("greet") == "1", "button": form.get("button") == "1",
             "to": "all" if to == "all" else "one"}
    return draft, people, None


@messages.get("/messages")
def compose():
    return _page()


@messages.post("/messages")
def send():
    s = svc()
    if not s.mailer.ready:
        return _page(request.form, "الإيميل غير مُعدّ بعد. أكمله من صفحة الإعدادات.", 400)
    if s.broadcasts.active():
        return _page(request.form, "رسالة أخرى قيد الإرسال الآن. انتظر حتى تكتمل أو أوقفها.", 409)
    draft, people, error = _draft(request.form)
    if error:
        return _page(request.form, error, 400)
    bid = s.broadcasts.create(draft, [p["id"] for p in people])
    start(current_app._get_current_object(), bid)
    current_app.logger.info("broadcast %s started to %s customers", bid, len(people))
    flash(f"بدأ الإرسال إلى {customers_ar(len(people))}. يمكنك إغلاق الصفحة، فالإرسال يكمل وحده.")
    return redirect(url_for(".compose"), 303)


@messages.post("/messages/test")
def test():
    s = svc()
    if not s.mailer.ready:
        return _page(request.form, "الإيميل غير مُعدّ بعد. أكمله من صفحة الإعدادات.", 400)
    if too_many("mailtest", 5):
        return _page(request.form, "طلبات كثيرة. انتظر دقيقة.", 429)
    draft, people, error = _draft(request.form)
    to = request.form.get("test_to", "").strip().lower() or s.cfg.owner_email
    if not error and (len(to) > 254 or not MAIL_ADDRESS.match(to)):
        error = "اكتب بريداً صحيحاً لرسالة التجربة."
    if error:
        return _page(request.form, error, 400)
    try:
        s.mailer.send(to, *letter(s, draft, people[0]["name"], f"{s.cfg.base_url}/unsubscribe/sample", trial=True))
    except MailError as exc:
        current_app.logger.warning("broadcast test failed: %s", exc.kind)
        return _page(request.form, f"لم تُرسل رسالة التجربة: {exc.hint}", 502)
    name = people[0]["name"] or "العميل"
    return _page(request.form, note=f"أُرسلت رسالة التجربة إلى {to} كما تصل إلى {name}. إن لم تجدها فتفقّد البريد غير المرغوب فيه.")


@messages.post("/messages/<bid>/stop")
def stop(bid):
    s = svc()
    s.broadcasts.get(bid) or abort(404)
    if s.broadcasts.request_stop(bid):
        flash("يتوقف الإرسال بعد الرسالة الحالية. يمكنك إكماله لاحقاً لمن بقي.")
    return redirect(url_for(".compose"), 303)


@messages.post("/messages/<bid>/resume")
def resume(bid):
    s = svc()
    run = s.broadcasts.get(bid) or abort(404)
    if not s.mailer.ready:
        flash("الإيميل غير مُعدّ بعد. أكمله من صفحة الإعدادات.", "error")
    elif s.broadcasts.active():
        flash("رسالة أخرى قيد الإرسال الآن. انتظر حتى تكتمل أو أوقفها.", "error")
    elif start(current_app._get_current_object(), bid):
        flash(f"يكمل الإرسال إلى من بقي: {run['left']}.")
    return redirect(url_for(".compose"), 303)


# ---------------------------------------------------------------- the customer's link
@leave.before_request
def limit():
    if too_many("leave", 30):
        abort(429)


@leave.get("/unsubscribe/sample")
def sample():
    return render_template("unsubscribe.html", sample=True, brand=svc().brand.get())


@leave.route("/unsubscribe/<cid>/<key>", methods=["GET", "POST"])
def unsubscribe(cid, key):
    """GET asks, POST stops the messages. Mail apps POST here straight from their own button (RFC 8058)."""
    s = svc()
    if not CID_RE.match(cid) or not hmac.compare_digest(key.encode(), leave_key(s.cfg, cid).encode()):
        abort(404)
    who = s.book.get(cid)
    if request.method == "POST":
        if who and not who["optout"]:
            s.book.stop_mail(cid)
            current_app.logger.info("customer %s stopped the messages", cid[:8])
    stopped = request.method == "POST" or not who or who["optout"]
    return render_template("unsubscribe.html", stopped=stopped, email=mask(who["email"]) if who else "",
                           brand=s.brand.get())
