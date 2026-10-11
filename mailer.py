"""Email through the shop's own mailbox (SMTP): the owner's sign-in code, a notice for every payment and the
customer's order. The connection is always encrypted and the server certificate is checked. Addresses, codes
and message contents are never written to the logs."""
import logging
import os
import smtplib
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from validate import MAIL_ADDRESS

log = logging.getLogger(__name__)
# Render's free instances cannot open connections to mail ports (25, 465, 587); paid instances can.
ON_RENDER = os.environ.get("RENDER", "").lower() == "true"
RENDER_FREE = ("تعذر الاتصال بخادم البريد. إن كانت خدمتك في Render على الخطة المجانية (Free) فـ Render يمنع فيها "
               "منافذ البريد 465 و 587، ويلزم نقلها إلى خطة مدفوعة. وإلا فتأكد من SMTP_SERVER و SMTP_PORT.")
HINTS = {
    "off": "الإيميل غير مُعدّ. أضف متغيرات SMTP في Render.",
    "address": "عنوان البريد غير صالح للإرسال.",
    "auth": "خادم البريد رفض SMTP_EMAIL أو SMTP_PASSWORD. تأكد منهما في Render.",
    "connect": "تعذر الاتصال بخادم البريد. تأكد من SMTP_SERVER و SMTP_PORT في Render.",
    "tls": "تعذر فتح اتصال آمن مع خادم البريد.",
    "refused": "خادم البريد رفض الرسالة أو عنوان المستلم.",
    "unsure": "انقطع الاتصال بخادم البريد أثناء الإرسال، فقد تكون الرسالة وصلت. تأكد قبل أن تعيد الإرسال.",
    "failed": "تعذر إرسال الإيميل. حاول مرة أخرى بعد قليل.",
}


class MailError(Exception):
    def __init__(self, kind):
        self.kind = kind if kind in HINTS else "failed"
        super().__init__(self.kind)

    @property
    def hint(self):
        return hint(self.kind)


def hint(kind):
    kind = kind if kind in HINTS else "failed"
    return RENDER_FREE if kind == "connect" and ON_RENDER else HINTS[kind]


def mask(address):
    """ab•••@gmail.com: enough for customers to recognise their own address, not enough to read it."""
    local, at, domain = (address or "").partition("@")
    return f"{local[:2]}•••@{domain}" if at and local and domain else ""


def one_line(text, limit=150):
    return " ".join(str(text or "").split())[:limit]


class Mailer:
    # If a port cannot be reached at all, the other standard secure port is tried once.
    OTHER_PORT = {465: 587, 587: 465}
    SEND_TIMEOUT = 25  # once the message is being handed over, the server gets longer to answer

    def __init__(self, cfg, context=None, timeout=10):
        self.host, self.port = cfg.smtp_server, cfg.smtp_port
        self.user, self.password = cfg.smtp_email, cfg.smtp_password
        self.name, self.ready = cfg.store_name, cfg.mail_ready
        self.context = context or ssl.create_default_context()
        self.timeout = timeout
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mail")
        self._jobs, self._lock = set(), threading.Lock()

    def compose(self, to, subject, text, html=None):
        msg = EmailMessage()
        msg["Subject"] = one_line(subject)
        msg["From"] = Address(one_line(self.name, 60), addr_spec=self.user)
        msg["To"] = to
        msg["Date"] = formatdate(usegmt=True)
        msg["Message-ID"] = make_msgid(domain=self.user.rpartition("@")[2])
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        return msg

    def send(self, to, subject, text, html=None):
        """Sends one message to one address now. Raises MailError with a reason the owner can act on."""
        if not self.ready:
            raise MailError("off")
        to = (to or "").strip()
        if len(to) > 254 or not MAIL_ADDRESS.match(to):
            raise MailError("address")
        msg = self.compose(to, subject, text, html)
        try:
            self._deliver(msg, to, self.port)
        except MailError as exc:
            other = self.OTHER_PORT.get(self.port)
            if exc.kind != "connect" or not other:
                raise
            self._deliver(msg, to, other)

    def _deliver(self, msg, to, port):
        server, handing, done = None, False, False
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(self.host, port, timeout=self.timeout, context=self.context)
            else:
                server = smtplib.SMTP(self.host, port, timeout=self.timeout)
                server.ehlo()
                if not server.has_extn("starttls"):
                    raise MailError("tls")  # never send the password over a plain connection
                server.starttls(context=self.context)
            server.login(self.user, self.password)
            server.sock.settimeout(self.SEND_TIMEOUT)
            handing = True
            server.send_message(msg, self.user, [to])
            done = True
        except MailError:
            raise
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPNotSupportedError):
            raise MailError("auth") from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise MailError("refused") from None
        except (smtplib.SMTPException, OSError) as exc:
            if handing:  # the message may already be with the server: it is never sent again automatically
                raise MailError("unsure") from None
            if isinstance(exc, ssl.SSLError):
                raise MailError("tls") from None
            if isinstance(exc, smtplib.SMTPException) and not isinstance(
                    exc, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)):
                raise MailError("failed") from None
            raise MailError("connect") from None
        finally:
            if server is not None:
                if done:
                    try:
                        server.quit()  # an odd answer to QUIT after a delivered message changes nothing
                    except Exception:
                        pass
                server.close()

    def later(self, job, *args):
        """Runs job(*args) on a mail thread, so a slow mail server never holds up a page or the gateway."""
        try:
            future = self._pool.submit(job, *args)
        except RuntimeError:  # the worker is shutting down
            return job(*args)
        with self._lock:
            self._jobs.add(future)
        future.add_done_callback(self._finished)
        return future

    def _finished(self, future):
        with self._lock:
            self._jobs.discard(future)
        failure = future.exception()
        if failure:
            log.error("mail job failed: %s", type(failure).__name__)

    def wait(self, timeout=30):
        """Waits for queued mail jobs (used by tests)."""
        with self._lock:
            jobs = list(self._jobs)
        wait(jobs, timeout)
