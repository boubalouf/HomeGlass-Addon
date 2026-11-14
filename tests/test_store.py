from __future__ import annotations

import copy
import json

import pytest

from homeglass_server.models.api import ProfileOverrides
from homeglass_server.store.assets import AssetError, AssetStore, detect_image_type
from homeglass_server.store.devices import DeviceStore
from homeglass_server.store.repository import LayoutStore, StoreError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def store(tmp_path) -> LayoutStore:
    return LayoutStore(tmp_path / "data", history_limit=20)


def test_publish_writes_revision_one_and_history(store, example_payload) -> None:
    document = store.publish(example_payload)

    assert document["revision"] == 1  # repart de zéro : rien n'était publié
    assert document["publishedAt"].endswith("Z")
    assert store.get_published()["revision"] == 1
    assert [version["revision"] for version in store.history()] == [1]


def test_publish_increments_revision_and_fills_every_field(store, example_payload) -> None:
    store.publish(example_payload)
    document = store.publish(example_payload)

    assert document["revision"] == 2
    # Tous les champs du contrat sont présents, y compris les null.
    assert set(document["theme"]["background"]) == {"type", "assetId", "blurDp", "dim"}
    assert document["theme"]["background"]["assetId"] is None


def test_history_is_limited_to_twenty_versions(tmp_path, example_payload) -> None:
    store = LayoutStore(tmp_path / "data", history_limit=20)
    for _ in range(25):
        store.publish(example_payload)

    versions = store.history()
    assert len(versions) == 20
    assert versions[0]["revision"] == 25
    assert versions[-1]["revision"] == 6
    assert sorted(path.name for path in (tmp_path / "data" / "layout" / "history").iterdir())[0] == "000006.json"


def test_restore_republishes_an_old_version_as_a_new_revision(store, example_payload) -> None:
    first = store.publish(example_payload)
    modified = copy.deepcopy(example_payload)
    modified["home"]["title"] = "Chez moi"
    store.publish(modified)

    restored = store.restore(1)

    assert restored["revision"] == 3
    assert restored["home"]["title"] == first["home"]["title"]
    assert store.get_published()["home"]["title"] == first["home"]["title"]


def test_restore_of_an_unknown_revision_fails(store) -> None:
    with pytest.raises(StoreError, match="absente"):
        store.restore(42)


def test_publishing_without_any_document_fails(tmp_path) -> None:
    with pytest.raises(StoreError, match="aucun brouillon"):
        LayoutStore(tmp_path / "data").publish()


def test_invalid_layout_is_rejected_and_nothing_is_written(store, example_payload) -> None:
    payload = copy.deepcopy(example_payload)
    payload["theme"]["accent"] = "orange"

    with pytest.raises(StoreError, match="mise en page invalide"):
        store.save_draft(payload)

    assert store.get_draft() is None
    assert store.get_published() is None


def test_future_schema_version_is_rejected_with_a_clear_message(store, example_payload) -> None:
    payload = copy.deepcopy(example_payload)
    payload["schemaVersion"] = 2

    with pytest.raises(StoreError, match="mettez à jour l'add-on"):
        store.publish(payload)


def test_draft_keeps_revision_and_can_be_published(store, example_payload) -> None:
    draft = store.save_draft(example_payload)

    assert draft["revision"] == 42
    published = store.publish()
    assert published["revision"] == 1
    assert published["home"] == draft["home"]


def test_writes_leave_no_temporary_file(store, example_payload) -> None:
    store.save_draft(example_payload)
    store.publish(example_payload)

    assert list((store.root).rglob("*.tmp")) == []


def test_device_profiles_override_the_published_layout(tmp_path, example_payload) -> None:
    devices = DeviceStore(tmp_path / "devices.json")
    devices.set_profile("default", {"hiddenTabs": ["auto"]})
    device = devices.register("dev_telephone", name="Téléphone", model="Pixel", app_version="1.0")
    devices.set_profile("dev_telephone", {"hiddenRooms": ["room_salon"], "themeMode": "dark"})

    assert device["deviceId"] == "dev_telephone"
    resolved = devices.apply_profile(example_payload, "dev_telephone")

    assert resolved["rooms"] == []
    assert resolved["theme"]["mode"] == "dark"
    assert next(tab for tab in resolved["tabs"] if tab["id"] == "auto")["enabled"] is False
    # Le document d'origine n'est jamais modifié.
    assert example_payload["rooms"][0]["id"] == "room_salon"
    assert example_payload["theme"]["mode"] == "auto"
    # Un appareil inconnu reçoit le profil par défaut.
    assert devices.apply_profile(example_payload, None)["rooms"] != []


def test_device_registration_generates_and_updates_identifiers(tmp_path) -> None:
    devices = DeviceStore(tmp_path / "devices.json")
    created = devices.register()
    again = devices.register(created["deviceId"], name="Salon")

    assert created["deviceId"].startswith("dev_")
    assert again["deviceId"] == created["deviceId"]
    assert again["name"] == "Salon"
    assert devices.count() == 1


def test_invalid_device_identifier_is_refused(tmp_path) -> None:
    devices = DeviceStore(tmp_path / "devices.json")

    with pytest.raises(ValueError, match="deviceId invalide"):
        devices.register("Tablette Du Salon")

    with pytest.raises(ValueError, match="deviceId invalide"):
        devices.register("default")


def test_profile_rejects_unknown_fields() -> None:
    with pytest.raises(Exception):
        ProfileOverrides.model_validate({"hiddenRooms": [], "inconnu": True})


def test_asset_store_detects_the_real_type(tmp_path) -> None:
    assets = AssetStore(tmp_path / "assets")

    meta = assets.save(PNG, filename="fond.png", declared_type="image/png")

    assert meta["contentType"] == "image/png"
    assert meta["filename"] == "fond.png"
    path, stored = assets.get(meta["id"])
    assert path.read_bytes() == PNG
    assert stored["id"] == meta["id"]


def test_asset_store_refuses_non_images_and_mismatched_types(tmp_path) -> None:
    assets = AssetStore(tmp_path / "assets")

    with pytest.raises(AssetError, match="format non pris en charge"):
        assets.save(b"<svg/>", declared_type="image/png")

    with pytest.raises(AssetError, match="l'en-tête annonce"):
        assets.save(PNG, declared_type="image/jpeg")

    with pytest.raises(AssetError, match="vide"):
        assets.save(b"")


def test_asset_store_refuses_oversized_files(tmp_path) -> None:
    assets = AssetStore(tmp_path / "assets", max_bytes=1024)

    with pytest.raises(AssetError, match="trop volumineux"):
        assets.save(PNG + b"0" * 2000)


def test_asset_store_deduplicates_identical_content(tmp_path) -> None:
    assets = AssetStore(tmp_path / "assets")

    first = assets.save(PNG, filename="a.png")
    second = assets.save(PNG, filename="b.png")

    assert first["id"] == second["id"]
    assert assets.count() == 1


def test_asset_lookup_rejects_path_traversal(tmp_path) -> None:
    assets = AssetStore(tmp_path / "assets")

    with pytest.raises(AssetError, match="invalide"):
        assets.get("../../etc/passwd")

    with pytest.raises(AssetError, match="inconnue"):
        assets.get("asset_0000000000000000")


def test_detect_image_type_recognizes_common_formats() -> None:
    assert detect_image_type(PNG) == ("image/png", ".png")
    assert detect_image_type(b"\xff\xd8\xff\xe0" + b"0" * 10) == ("image/jpeg", ".jpg")
    assert detect_image_type(b"GIF89a" + b"0" * 10) == ("image/gif", ".gif")
    assert detect_image_type(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"0" * 8) == ("image/webp", ".webp")
    assert detect_image_type(json.dumps({"a": 1}).encode()) is None
