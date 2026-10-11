"""Settings are read from environment variables only (Render -> Environment).
Nothing sensitive is stored in the repository."""
import base64
import json
import os

from validate import MAIL_ADDRESS

GATEWAYS = {
    "production": "https://app-api.edfapay.com",
    "sandbox": "https://demo-api.edfapay.com",
}


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def _service_account(raw):
    """Accepts the Firebase service-account JSON as-is or base64-encoded."""
    for read in (lambda: raw, lambda: base64.b64decode(raw).decode("utf-8")):
        try:
            data = json.loads(read())
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


class Config:
    def __init__(self):
        self.owner_number = _env("OWNER_NUMBER")
        self.owner_password = _env("OWNER_PASSWORD")
        self.owner_password_hash = _env("OWNER_PASSWORD_HASH")
        self.secret_key = _env("SECRET_KEY")
        self.encryption_key = _env("ENCRYPTION_KEY")
        self.service_account = _service_account(_env("FIREBASE_CREDENTIALS"))
        self.emulator = bool(_env("FIRESTORE_EMULATOR_HOST"))
        self.edfa_api_key = _env("EDFAPAY_API_KEY")
        self.edfa_webhook_secret = _env("EDFAPAY_WEBHOOK_SECRET")
        self.edfa_merchant_id = _env("EDFAPAY_MERCHANT_ID")
        self.edfa_merchant_password = _env("EDFAPAY_MERCHANT_PASSWORD")
        self.edfa_fallback_ip = _env("EDFAPAY_FALLBACK_IP", "127.0.0.1")
        # Accounts with a merchant ID + password use the older API; accounts with an API key use the newer one.
        self.legacy = bool(self.edfa_merchant_id or self.edfa_merchant_password) or not self.edfa_api_key
        self.edfa_env = _env("EDFAPAY_ENV", "sandbox").lower()
        default_url = "https://api.edfapay.com" if self.legacy else GATEWAYS.get(self.edfa_env, "")
        self.edfa_base_url = (_env("EDFAPAY_BASE_URL") or default_url).rstrip("/")
        self.base_url = (_env("BASE_URL") or _env("RENDER_EXTERNAL_URL")).rstrip("/")
        self.store_name = _env("STORE_NAME", "فواتير الدفع")
        self.insecure_dev = _env("INSECURE_DEV") == "1"
        # Email is optional: the shop's mailbox sends the sign-in code, payment notices and the customer's order.
        self.smtp_server = _env("SMTP_SERVER")
        port = _env("SMTP_PORT", "465")
        self.smtp_port = int(port) if port.isdigit() and 0 < int(port) < 65536 else 0
        self.smtp_email = _env("SMTP_EMAIL")
        self.smtp_password = _env("SMTP_PASSWORD")
        owner_email = _env("OWNER_EMAIL").lower()
        self.owner_email = owner_email if MAIL_ADDRESS.match(owner_email) else ""

    @property
    def mail_ready(self):
        return bool(self.smtp_server and self.smtp_port and self.smtp_password
                    and MAIL_ADDRESS.match(self.smtp_email))

    def mail_problems(self):
        """[(variable, what it is)] still missing for email. Email is optional, so this never stops the site."""
        out = []
        if not self.smtp_server:
            out.append(("SMTP_SERVER", "خادم البريد، مثل mail.privateemail.com"))
        if not self.smtp_port:
            out.append(("SMTP_PORT", "منفذ خادم البريد: 465 أو 587"))
        if not MAIL_ADDRESS.match(self.smtp_email):
            out.append(("SMTP_EMAIL", "الإيميل الذي تُرسل منه الرسائل"))
        if not self.smtp_password:
            out.append(("SMTP_PASSWORD", "كلمة مرور ذلك الإيميل"))
        if not self.owner_email:
            out.append(("OWNER_EMAIL", "إيميلك أنت: يصله رمز الدخول وإشعار كل دفعة"))
        return out

    def problems(self):
        """Returns [(variable, what is wrong)] - the site refuses to run until this is empty."""
        out = []
        if not self.owner_number:
            out.append(("OWNER_NUMBER", "رقم المالك المستخدم لتسجيل الدخول"))
        if not self.owner_password_hash and len(self.owner_password) < 10:
            out.append(("OWNER_PASSWORD", "كلمة مرور المالك، 10 خانات على الأقل"))
        if len(self.secret_key) < 32:
            out.append(("SECRET_KEY", "نص عشوائي من 32 خانة أو أكثر لتوقيع الجلسات"))
        if len(self.encryption_key) < 32:
            out.append(("ENCRYPTION_KEY", "نص عشوائي من 32 خانة أو أكثر لتشفير بيانات العملاء"))
        sa = self.service_account or {}
        if not self.emulator and not all(sa.get(k) for k in ("project_id", "private_key", "client_email")):
            out.append(("FIREBASE_CREDENTIALS", "ملف مفتاح حساب الخدمة من Firebase بصيغة JSON كاملاً"))
        if self.legacy:
            if not self.edfa_merchant_id:
                out.append(("EDFAPAY_MERCHANT_ID", "معرف التاجر من إعدادات التكامل في لوحة EdfaPay"))
            if not self.edfa_merchant_password:
                out.append(("EDFAPAY_MERCHANT_PASSWORD", "كلمة مرور التاجر من إعدادات التكامل في لوحة EdfaPay"))
        elif not self.edfa_webhook_secret:
            out.append(("EDFAPAY_WEBHOOK_SECRET", "السر الذي تكتبه في إعداد Webhook داخل لوحة EdfaPay"))
        if not self.edfa_base_url:
            out.append(("EDFAPAY_ENV", "اكتب sandbox أو production"))
        if not self.base_url or not (self.base_url.startswith("https://") or self.insecure_dev):
            out.append(("BASE_URL", "رابط الموقع ويبدأ بـ https://"))
        return out
