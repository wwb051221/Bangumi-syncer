from __future__ import annotations

import pytest

from bangumi_syncer.state import StateStore
from bangumi_syncer.ui import UiError, UiManager

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 128


@pytest.fixture()
def manager(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    instance = UiManager(store, str(tmp_path))
    try:
        yield instance, store, tmp_path
    finally:
        store.close()


def test_defaults(manager):
    instance, _store, _tmp = manager
    ui = instance.public()
    assert ui["theme"] == "auto"
    assert ui["accent"] == "#7db3ff"
    assert ui["background"]["mode"] == "none"
    assert ui["palette"] == []
    assert ui["rev"] == 0


def test_update_persists_and_bumps_rev(manager):
    instance, store, _tmp = manager
    ui = instance.update({"theme": "dark", "accent": "#6ee7a8", "font_scale": 1.1, "density": "compact"})
    assert ui["theme"] == "dark"
    assert ui["rev"] == 1

    reloaded = UiManager(store, str(_tmp))
    assert reloaded.public()["theme"] == "dark"
    assert reloaded.public()["accent"] == "#6ee7a8"
    assert reloaded.public()["density"] == "compact"


@pytest.mark.parametrize("payload,needle", [
    ({"theme": "neon"}, "theme"),
    ({"accent": "red"}, "accent"),
    ({"density": "loose"}, "density"),
    ({"background": {"mode": "video"}}, "background.mode"),
    ({"palette": ["nope"]}, "palette"),
])
def test_validation(manager, payload, needle):
    instance, _store, _tmp = manager
    with pytest.raises(UiError) as excinfo:
        instance.update(payload)
    assert needle in str(excinfo.value)


def test_numbers_are_clamped(manager):
    instance, _store, _tmp = manager
    ui = instance.update({"font_scale": 9, "background": {"opacity": 5, "blur": 999, "dim": -3}})
    assert ui["font_scale"] == 1.4
    assert ui["background"]["opacity"] == 1.0
    assert ui["background"]["blur"] == 30.0
    assert ui["background"]["dim"] == 0.0


def test_preset_background(manager):
    instance, _store, _tmp = manager
    ui = instance.use_preset("ocean")
    assert ui["background"]["mode"] == "preset"
    assert ui["background"]["preset"] == "ocean"
    assert ui["background"]["url"] == ""


def test_image_background_and_palette(manager):
    instance, _store, tmp_path = manager
    ui = instance.set_background_bytes(PNG, "image/png")
    assert ui["background"]["mode"] == "image"
    assert ui["background"]["content_type"] == "image/png"
    assert ui["background"]["url"].startswith("/static/background?v=")

    payload, content_type, version = instance.background()
    assert payload == PNG and content_type == "image/png" and version >= 1

    ui = instance.set_palette(["#aabbcc", "#AABBCC", "#112233"])
    assert ui["palette"] == ["#aabbcc", "#112233"]

    ui = instance.clear_background()
    assert ui["background"]["mode"] == "none"
    assert instance.background() is None


def test_jpeg_detection(manager):
    instance, _store, _tmp = manager
    assert instance.set_background_bytes(JPEG)["background"]["content_type"] == "image/jpeg"


def test_invalid_image_rejected(manager):
    instance, _store, _tmp = manager
    with pytest.raises(UiError) as excinfo:
        instance.set_background_bytes(b"not an image at all")
    assert "PNG" in str(excinfo.value)


def test_url_scheme_is_validated(manager):
    instance, _store, _tmp = manager
    with pytest.raises(UiError):
        instance.set_background_url("file:///etc/passwd")


def test_background_requires_an_upload(manager):
    instance, _store, _tmp = manager
    with pytest.raises(UiError) as excinfo:
        instance.update({"background": {"mode": "image"}})
    assert "no background image" in str(excinfo.value)


def test_missing_image_falls_back_on_start(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    instance = UiManager(store, str(tmp_path))
    instance.set_background_bytes(PNG)
    (tmp_path / "assets" / "background.bin").unlink()
    reloaded = UiManager(store, str(tmp_path))
    assert reloaded.public()["background"]["mode"] == "none"
    store.close()
