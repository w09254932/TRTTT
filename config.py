"""Settings are read from environment variables only (Render -> Environment).
Nothing sensitive is stored in the repository."""
import base64
import json
import os

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
        self.edfa_env = _env("EDFAPAY_ENV", "sandbox").lower()
        self.edfa_base_url = (_env("EDFAPAY_BASE_URL") or GATEWAYS.get(self.edfa_env, "")).rstrip("/")
        self.base_url = (_env("BASE_URL") or _env("RENDER_EXTERNAL_URL")).rstrip("/")
        self.store_name = _env("STORE_NAME", "فواتير الدفع")
        self.insecure_dev = _env("INSECURE_DEV") == "1"

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
        if not self.edfa_api_key:
            out.append(("EDFAPAY_API_KEY", "مفتاح API من لوحة EdfaPay"))
        if not self.edfa_webhook_secret:
            out.append(("EDFAPAY_WEBHOOK_SECRET", "السر الذي تكتبه في إعداد Webhook داخل لوحة EdfaPay"))
        if not self.edfa_base_url:
            out.append(("EDFAPAY_ENV", "اكتب sandbox أو production"))
        if not self.base_url or not (self.base_url.startswith("https://") or self.insecure_dev):
            out.append(("BASE_URL", "رابط الموقع ويبدأ بـ https://"))
        return out
