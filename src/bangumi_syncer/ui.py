"""Server-side UI personalisation: theme, accent, density, background image.

Everything the user can change in the "外观" section lives here and is stored in
the state database (``meta.ui_prefs``) plus an image file under
``<state_dir>/assets/``. That makes the look identical on every device instead
of being tied to one browser's localStorage; the browser keeps a small cache
only to avoid a flash of the wrong theme on first paint.

The Morandi palette is extracted in the browser (canvas can decode every format
the user might upload) and posted back, so the server stays dependency free.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

from .log import get_logger
from .state import StateStore

log = get_logger("ui")

PREFS_KEY = "ui_prefs"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PALETTE = 8
THEMES = ("auto", "light", "dark")
DENSITIES = ("comfortable", "compact")
PRESETS = ("mist", "dusk", "ocean", "paper")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULT_PREFS: dict[str, Any] = {
    "theme": "auto",
    "accent": "#7db3ff",
    "font_scale": 1.0,
    "density": "comfortable",
    "background": {
        "mode": "none",       # none | image | preset
        "preset": "mist",
        "opacity": 0.55,
        "blur": 8,
        "dim": 0.45,
        "version": 0,
        "content_type": "",
    },
    "palette": [],
    "rev": 0,
}


class UiError(Exception):
    """Invalid personalisation payload (HTTP 400)."""


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(base))  # deep copy
    for key, value in patch.items():
        if key == "background" and isinstance(value, dict):
            out["background"].update({k: v for k, v in value.items() if v is not None})
        elif value is not None:
            out[key] = value
    return out


def _clamp(value: Any, low: float, high: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UiError("%s must be a number" % name)
    return round(min(high, max(low, float(value))), 3)


def _detect_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class UiManager:
    def __init__(self, store: StateStore, data_dir: str, user_agent: str = "bangumi-syncer") -> None:
        self.store = store
        self.assets = Path(data_dir) / "assets"
        self.assets.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        stored = store.get_json(PREFS_KEY)
        self._prefs = _merge(DEFAULT_PREFS, stored if isinstance(stored, dict) else {})
        if self._prefs["background"]["mode"] == "image" and not self._image_path().exists():
            log.warning("background image is missing on disk, falling back to none")
            self._prefs["background"]["mode"] = "none"
            self._persist()

    # ------------------------------------------------------------------ read
    def public(self) -> dict[str, Any]:
        prefs = json.loads(json.dumps(self._prefs))
        background = prefs["background"]
        if background["mode"] == "image" and self._image_path().exists():
            background["url"] = "/static/background?v=%d" % int(background.get("version") or 0)
        else:
            background["url"] = ""
        background["image_bytes"] = self._image_path().stat().st_size if self._image_path().exists() else 0
        return prefs

    def background(self) -> tuple[bytes, str, int] | None:
        background = self._prefs["background"]
        if background["mode"] != "image":
            return None
        path = self._image_path()
        if not path.exists():
            return None
        content_type = background.get("content_type") or "application/octet-stream"
        return path.read_bytes(), content_type, int(background.get("version") or 0)

    # ----------------------------------------------------------------- write
    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise UiError("expected a JSON object")
        prefs = _merge(self._prefs, payload)

        if prefs["theme"] not in THEMES:
            raise UiError("theme must be one of %s" % ", ".join(THEMES))
        if not isinstance(prefs["accent"], str) or not HEX.match(prefs["accent"]):
            raise UiError("accent must be a #rrggbb colour")
        prefs["font_scale"] = _clamp(prefs["font_scale"], 0.8, 1.4, "font_scale")
        if prefs["density"] not in DENSITIES:
            raise UiError("density must be one of %s" % ", ".join(DENSITIES))
        if not isinstance(prefs["palette"], list) or any(
                not isinstance(color, str) or not HEX.match(color) for color in prefs["palette"]):
            raise UiError("palette must be a list of #rrggbb colours")
        if len(prefs["palette"]) > MAX_PALETTE:
            raise UiError("palette has too many colours (max %d)" % MAX_PALETTE)

        background = prefs["background"]
        if background["mode"] not in ("none", "image", "preset"):
            raise UiError("background.mode must be none, image or preset")
        if background["preset"] not in PRESETS:
            background["preset"] = PRESETS[0]
        background["opacity"] = _clamp(background["opacity"], 0.05, 1.0, "background.opacity")
        background["blur"] = round(_clamp(background["blur"], 0.0, 30.0, "background.blur"), 1)
        background["dim"] = _clamp(background["dim"], 0.0, 0.9, "background.dim")
        if background["mode"] == "image" and not self._image_path().exists():
            raise UiError("no background image uploaded yet")

        prefs["rev"] = int(self._prefs.get("rev") or 0) + 1
        self._prefs = prefs
        self._persist()
        log.info("ui prefs updated (rev=%d, theme=%s, background=%s)",
                 prefs["rev"], prefs["theme"], background["mode"])
        return self.public()

    def set_palette(self, colors: Any) -> dict[str, Any]:
        if not isinstance(colors, list) or not colors or len(colors) > MAX_PALETTE:
            raise UiError("colors must be a non-empty list (max %d)" % MAX_PALETTE)
        palette: list[str] = []
        for color in colors:
            if not isinstance(color, str) or not HEX.match(color):
                raise UiError("invalid colour %r" % (color,))
            if color.lower() not in [c.lower() for c in palette]:
                palette.append(color.lower())
        return self.update({"palette": palette})

    def set_background_bytes(self, data: bytes, content_type: str | None = None) -> dict[str, Any]:
        if not data:
            raise UiError("empty image")
        if len(data) > MAX_IMAGE_BYTES:
            raise UiError("image is larger than %d MB" % (MAX_IMAGE_BYTES // (1024 * 1024)))
        detected = _detect_image(data)
        if detected is None:
            raise UiError("only PNG, JPEG, WebP and GIF images are supported")
        if content_type and not content_type.startswith("image/"):
            raise UiError("unexpected content type %r" % content_type)
        self._write_image(data)
        background = dict(self._prefs["background"])
        background.update({
            "mode": "image",
            "content_type": detected,
            "version": int(background.get("version") or 0) + 1,
        })
        return self.update({"background": background})

    def set_background_url(self, url: str) -> dict[str, Any]:
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            raise UiError("url must start with http:// or https://")
        data = self._download(url)
        return self.set_background_bytes(data)

    def use_preset(self, preset: str) -> dict[str, Any]:
        if preset not in PRESETS:
            raise UiError("unknown preset %r" % preset)
        background = dict(self._prefs["background"])
        background.update({"mode": "preset", "preset": preset})
        return self.update({"background": background})

    def clear_background(self) -> dict[str, Any]:
        background = dict(self._prefs["background"])
        background.update({"mode": "none", "version": int(background.get("version") or 0) + 1})
        self._image_path().unlink(missing_ok=True)
        return self.update({"background": background})

    # ----------------------------------------------------------------- utils
    def _image_path(self) -> Path:
        return self.assets / "background.bin"

    def _write_image(self, data: bytes) -> None:
        tmp = self.assets / "background.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, self._image_path())

    def _persist(self) -> None:
        self.store.set_json(PREFS_KEY, self._prefs)

    def _download(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                chunk = response.read(MAX_IMAGE_BYTES + 1)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            raise UiError("could not download image: %s" % exc) from exc
        if len(chunk) > MAX_IMAGE_BYTES:
            raise UiError("image is larger than %d MB" % (MAX_IMAGE_BYTES // (1024 * 1024)))
        return chunk
