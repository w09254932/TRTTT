"""Encryption, owner login, CSRF, request throttling and response headers."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import abort, request, session
from werkzeug.security import check_password_hash

from validate import western

SESSION_MAX_AGE = 24 * 3600
CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
       "form-action 'self' https://*.edfapay.com; base-uri 'none'; frame-ancestors 'none'")


class Vault:
    """AES-256-GCM encryption for customer data before it is written to Firestore.
    Each record is bound to its invoice id, so a blob copied to another invoice will not open."""

    def __init__(self, secret):
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"trttt-vault-v1").derive(secret.encode())
        self._aes = AESGCM(key)
        self._index_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                               info=b"trttt-index-v1").derive(secret.encode())

    def index(self, value):
        """Keyed fingerprint of a value: lets a record be found without storing the value itself."""
        return hmac.new(self._index_key, value.encode(), hashlib.sha256).hexdigest()[:32]

    def seal(self, data, bound_to):
        nonce = os.urandom(12)
        plain = json.dumps(data, ensure_ascii=False).encode()
        return "v1." + base64.urlsafe_b64encode(nonce + self._aes.encrypt(nonce, plain, bound_to.encode())).decode()

    def open(self, blob, bound_to):
        try:
            raw = base64.urlsafe_b64decode(blob[3:])
            return json.loads(self._aes.decrypt(raw[:12], raw[12:], bound_to.encode()))
        except Exception:
            return {}


def _same(a, b):
    return hmac.compare_digest(hashlib.sha256(a.encode()).digest(), hashlib.sha256(b.encode()).digest())


def check_owner(cfg, number, password):
    number_ok = _same(western(number).replace(" ", ""), western(cfg.owner_number).replace(" ", ""))
    if cfg.owner_password_hash:
        password_ok = check_password_hash(cfg.owner_password_hash, password)
    else:
        password_ok = _same(password, cfg.owner_password)
    return number_ok and password_ok


def fingerprint(cfg):
    """Changes when the owner number or password changes, which signs every old session out."""
    material = f"{cfg.owner_number}|{cfg.owner_password_hash or cfg.owner_password}".encode()
    return hmac.new(cfg.secret_key.encode(), material, hashlib.sha256).hexdigest()[:32]


def sign_in(cfg):
    session.clear()
    session.permanent = True
    session.update(uid="owner", fp=fingerprint(cfg), t=int(time.time()), csrf=secrets.token_urlsafe(24))


def signed_in(cfg):
    return (session.get("uid") == "owner"
            and hmac.compare_digest(str(session.get("fp", "")), fingerprint(cfg))
            and time.time() - session.get("t", 0) < SESSION_MAX_AGE)


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf():
    sent, kept = request.form.get("csrf", ""), session.get("csrf", "")
    if not sent or not kept or not _same(sent, kept):
        abort(400, "انتهت صلاحية الصفحة. ارجع وحدّث الصفحة ثم حاول مرة أخرى.")


def client_ip():
    direct = request.headers.get("CF-Connecting-IP", "").strip()
    if direct:
        return direct[:64]
    hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
    if len(hops) >= 2:
        return hops[-2][:64]
    return (hops[0] if hops else request.remote_addr or "unknown")[:64]


def ip_key(cfg, ip=None):
    """Addresses are stored only as a keyed hash."""
    return hmac.new(cfg.secret_key.encode(), (ip or client_ip()).encode(), hashlib.sha256).hexdigest()[:40]


_hits = {}


def too_many(bucket, limit, per=60):
    """Small in-memory limiter per address; the Firestore login lock is the durable one."""
    now, key = time.time(), (bucket, client_ip())
    recent = [t for t in _hits.get(key, ()) if now - t < per]
    recent.append(now)
    if len(_hits) > 5000:
        _hits.clear()
    _hits[key] = recent
    return len(recent) > limit


def harden(response, secure):
    h = response.headers
    h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "no-referrer"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["X-Robots-Tag"] = "noindex, nofollow"
    if secure:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if not request.path.startswith("/static/"):
        h.setdefault("Cache-Control", "no-store")
    return response
