"""EdfaPay hosted checkout client for accounts with an API key (docs.edfapay.com and the live Swagger at
app-api.edfapay.com/api-docs). The two disagree on some paths, so each call tries the Swagger path first
and the documented one when the first is not found."""
import base64
import binascii
import hashlib
import hmac
import logging
import re
from urllib.parse import quote, urlparse

import requests


log = logging.getLogger(__name__)


class GatewayError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


INITIATE_PATHS = ("/api/v1/payment/initiate", "/api/v1/payment-gateway/initiate")
NOT_THERE = (404, 405)


class EdfaPay:
    legacy = False

    def __init__(self, base_url, api_key, webhook_secret, timeout=20):
        self.base_url = base_url
        self.webhook_secret = webhook_secret.encode()
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers["X-API-KEY"] = api_key

    def _call(self, method, path, **kwargs):
        try:
            resp = self.http.request(method, self.base_url + path, timeout=self.timeout, **kwargs)
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise GatewayError(f"no usable answer from gateway: {type(exc).__name__}") from exc
        if not isinstance(body, dict) or resp.status_code != 200 or body.get("code") != 200:
            message = body.get("message") if isinstance(body, dict) else ""
            raise GatewayError(f"HTTP {resp.status_code}: {str(message)[:300]}", resp.status_code)
        return body.get("data") or {}

    def _first(self, method, paths, **kwargs):
        """Calls the first of `paths` that exists on the gateway."""
        for path in paths:
            try:
                return self._call(method, path, **kwargs)
            except GatewayError as exc:
                if exc.status not in NOT_THERE or path == paths[-1]:
                    raise
        return {}

    def initiate(self, order_id, amount, customer, success_url, failure_url, payer_ip=None, items=None, title=""):
        """Opens a checkout session and returns the URL the customer must be sent to. With `items` the checkout
        page lists the order's products; if EdfaPay refuses that list, the payment opens without it."""
        body = {
            "orderId": order_id,
            "currency": "SAR",
            "amount": amount,
            "customerDetails": {"name": customer["name"], "email": customer["email"], "phone": customer["phone"]},
            "auth": "N",
            "successUrl": success_url,
            "failureUrl": failure_url,
        }
        extra = {}
        if title:
            extra["description"] = str(title)[:120]
        if items:
            extra["invoice"] = {"shippingCharges": 0, "extraCharges": 0, "extraDiscount": 0, "total": amount,
                                "lineItems": items}
        try:
            data = self._first("POST", INITIATE_PATHS, json={**body, **extra})
        except GatewayError as exc:
            if not extra or exc.status != 400:
                raise
            log.warning("edfapay refused the order details (%s); opening the payment without them", exc)
            data = self._first("POST", INITIATE_PATHS, json=body)
        url = str(data.get("redirectUrl") or "")
        parts = urlparse(url)
        host = parts.hostname or ""
        if parts.scheme != "https" or not (host == "edfapay.com" or host.endswith(".edfapay.com")):
            raise GatewayError("gateway returned an unexpected redirect URL")
        return url

    def status(self, transaction_id):
        """Looks a transaction up by EdfaPay's transactionId. Returns its record or None."""
        try:
            data = self._call("GET", f"/api/v1/transactions/{quote(str(transaction_id), safe='')}/details")
            record = data.get("transactionDetails") if isinstance(data, dict) else None
            if isinstance(record, dict) and record:
                return record
        except GatewayError as exc:
            if exc.status not in NOT_THERE:
                raise
        data = self._call("GET", "/api/v1/transactions/filterTransaction", params={"id": transaction_id})
        content = data.get("content") or []
        return content[0] if content and isinstance(content[0], dict) else None

    def _digest(self, raw_body, algo=hashlib.sha256):
        return hmac.new(self.webhook_secret, raw_body, algo).digest()

    def verify(self, raw_body, signature):
        """X-EdfaPay-Signature must be the HMAC-SHA256 of the raw body with the webhook secret: hex as documented
        (either letter case), or the same digest in base64. Both need the secret."""
        given = (signature or "").strip()
        given = given[7:] if given.lower().startswith("sha256=") else given
        if not given or not self.webhook_secret:
            return False
        digest = self._digest(raw_body)
        if hmac.compare_digest(digest.hex().encode(), given.lower().encode()):
            return True
        try:
            sent = base64.b64decode(given, validate=True)
        except (binascii.Error, ValueError):
            return False
        return hmac.compare_digest(digest, sent)

    def diagnose(self, raw_body, headers, signature):
        """Why a notice was refused, for the log only: sizes, shapes and which signing would have matched.
        Never the secret, the signature or the body themselves."""
        sig = (signature or "").strip()
        sig = sig[7:] if sig.lower().startswith("sha256=") else sig
        shape = ("none" if not sig else "hex" if re.fullmatch(r"[0-9a-fA-F]+", sig)
                 else "base64" if re.fullmatch(r"[A-Za-z0-9+/=_-]+", sig) else "other")
        tries = {
            "hmac256-raw": self._digest(raw_body),
            "hmac256-trimmed-body": self._digest(raw_body.strip()),
            "hmac512-raw": self._digest(raw_body, hashlib.sha512),
            "hmac1-raw": self._digest(raw_body, hashlib.sha1),
            "sha256-body+secret": hashlib.sha256(raw_body + self.webhook_secret).digest(),
            "sha256-secret+body": hashlib.sha256(self.webhook_secret + raw_body).digest(),
        }
        matches = []
        for name, digest in tries.items():
            forms = (digest.hex(), base64.b64encode(digest).decode(), base64.urlsafe_b64encode(digest).decode())
            if sig and any(hmac.compare_digest(f.encode(), (sig.lower() if i == 0 else sig).encode())
                           for i, f in enumerate(forms)):
                matches.append(name)
        named = sorted(h for h in headers.keys() if re.search(r"sign|hash|hmac|auth|token|secret", h, re.I))
        return (f"body {len(raw_body)} bytes ({headers.get('Content-Type', '-')}), signature {shape} "
                f"length {len(sig)}, headers like a signature: {', '.join(named) or '-'}, "
                f"secret length {len(self.webhook_secret)} id {hashlib.sha256(self.webhook_secret).hexdigest()[:8]}, "
                f"would match: {', '.join(matches) or 'nothing'}")
