"""Shop logo and colours for the customer pages. They are kept in Firestore and served from this
site only, as a stylesheet and an image, so the strict content security policy stays as it is."""
import hashlib
import re
import time

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for

from security import too_many
from views_admin import guard

brand = Blueprint("brand", __name__)
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULTS = {"primary": "#0b6e5a", "background": "#edf0f4"}
MAX_LOGO = 300 * 1024
NO_LOGO = "الشعار يجب أن يكون صورة PNG أو JPG أو WebP بحجم أقل من 300 كيلوبايت."


def image_type(data):
    """Only real raster images are accepted, judged by their first bytes and never by the file name."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _rgb(color):
    return [int(color[i:i + 2], 16) for i in (1, 3, 5)]


def _mix(color, other, share):
    return "#%02x%02x%02x" % tuple(round(a + (b - a) * share) for a, b in zip(_rgb(color), _rgb(other)))


def _luminance(color):
    parts = [c / 255 for c in _rgb(color)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _readable(color, on, target):
    """Moves a colour toward black or white until text in it can be read on the given background."""
    toward = "#000000" if _luminance(on) > 0.4 else "#ffffff"
    for step in range(11):
        candidate = _mix(color, toward, step / 10)
        if _contrast(candidate, on) >= target:
            return candidate
    return toward


def theme_css(primary, background):
    on_primary = "#ffffff" if _contrast("#ffffff", primary) >= _contrast("#14213d", primary) else "#14213d"
    ink = _readable(primary, "#ffffff", 3.5)
    quiet = _readable("#586377", background, 4.5)
    return (f":root{{--riyal:{primary};--paper:{background}}}"
            f".primary{{color:{on_primary}}}.primary:hover{{background:{_mix(primary, '#000000', 0.15)}}}"
            f".receipt .issuer,.stamp{{color:{ink}}}.stamp{{border-color:{ink}}}.fine{{color:{quiet}}}")


class Brand:
    def __init__(self, client, ttl=60):
        self.ref, self.ttl = client.collection("meta").document("brand"), ttl
        self._data, self._at = None, 0

    def get(self, fresh=False):
        if fresh or self._data is None or time.time() - self._at > self.ttl:
            raw = self.ref.get().to_dict() or {}
            data = {k: str(raw.get(k)) if HEX.match(str(raw.get(k) or "")) else DEFAULTS[k] for k in DEFAULTS}
            logo = bytes(raw.get("logo") or b"")
            data["logo_type"] = image_type(logo)
            data["logo"] = logo if data["logo_type"] else None
            data["custom"] = any(data[k] != DEFAULTS[k] for k in DEFAULTS)
            seed = (data["primary"] + data["background"]).encode() + (data["logo"] or b"")
            data["version"] = hashlib.sha256(seed).hexdigest()[:12]
            self._data, self._at = data, time.time()
        return self._data

    def save(self, primary, background, logo=None, remove_logo=False):
        changes = {"primary": primary, "background": background}
        if logo:
            changes["logo"] = logo
        elif remove_logo:
            changes["logo"] = None
        self.ref.set(changes, merge=True)
        return self.get(fresh=True)


def svc():
    return current_app.extensions["svc"]


@brand.before_request
def protect():
    if request.endpoint in ("brand.settings", "brand.save"):
        if request.endpoint == "brand.save":
            request.max_content_length = MAX_LOGO + 64 * 1024
        return guard()
    if too_many("public", 120):
        abort(429)


def _page(error=None, code=200):
    from twostep import status
    return render_template("settings.html", brand=svc().brand.get(fresh=error is None), error=error,
                           mail=status(svc())), code


@brand.get("/settings")
def settings():
    return _page()


@brand.post("/settings")
def save():
    form = request.form
    primary, background = form.get("primary", "").strip().lower(), form.get("background", "").strip().lower()
    upload = request.files.get("logo")
    logo = upload.read(MAX_LOGO + 1) if upload and upload.filename else b""
    error = None
    if not HEX.match(primary) or not HEX.match(background):
        error = "اختر اللونين من منتقي الألوان."
    elif logo and (len(logo) > MAX_LOGO or not image_type(logo)):
        error = NO_LOGO
    if error:
        return _page(error, 400)
    svc().brand.save(primary, background, logo or None, form.get("remove_logo") == "1")
    flash("تم حفظ الشعار والألوان.")
    return redirect(url_for(".settings"), 303)


def _served():
    """The current look, re-read when the page asks for a newer version than this worker has cached."""
    data = svc().brand.get()
    if request.args.get("v") != data["version"]:
        data = svc().brand.get(fresh=True)
    lasting = request.args.get("v") == data["version"]
    return data, {"Cache-Control": "public, max-age=604800, immutable" if lasting else "no-store"}


@brand.get("/brand/logo")
def logo():
    data, headers = _served()
    if not data["logo"]:
        abort(404)
    return Response(data["logo"], mimetype=data["logo_type"], headers=headers)


@brand.get("/brand/theme.css")
def theme():
    data, headers = _served()
    return Response(theme_css(data["primary"], data["background"]), mimetype="text/css", headers=headers)
