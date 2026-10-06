"""EdfaPay hosted checkout client (docs.edfapay.com, API v2.0)."""
import hashlib
import hmac
from urllib.parse import urlparse

import requests


class GatewayError(Exception):
    pass


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
            raise GatewayError(f"HTTP {resp.status_code}: {str(message)[:300]}")
        return body.get("data") or {}

    def initiate(self, order_id, amount, customer, success_url, failure_url, payer_ip=None):
        """Opens a checkout session and returns the URL the customer must be sent to."""
        data = self._call("POST", "/api/v1/payment-gateway/initiate", json={
            "orderId": order_id,
            "currency": "SAR",
            "amount": amount,
            "customerDetails": {"name": customer["name"], "email": customer["email"], "phone": customer["phone"]},
            "auth": "N",
            "successUrl": success_url,
            "failureUrl": failure_url,
        })
        url = str(data.get("redirectUrl") or "")
        parts = urlparse(url)
        host = parts.hostname or ""
        if parts.scheme != "https" or not (host == "edfapay.com" or host.endswith(".edfapay.com")):
            raise GatewayError("gateway returned an unexpected redirect URL")
        return url

    def status(self, transaction_id):
        """Looks a transaction up by EdfaPay's transactionId. Returns its record or None."""
        data = self._call("GET", "/api/v1/transactions/filterTransaction", params={"id": transaction_id})
        content = data.get("content") or []
        return content[0] if content and isinstance(content[0], dict) else None

    def verify(self, raw_body, signature):
        """X-EdfaPay-Signature must be the hex HMAC-SHA256 of the raw body."""
        expected = hmac.new(self.webhook_secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected.encode(), (signature or "").strip().lower().encode())
