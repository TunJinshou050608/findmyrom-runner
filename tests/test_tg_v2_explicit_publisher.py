"""Tests for explicit ADD/UPDATE/WITHDRAW D1 publisher (v2 Section 3.6)."""
import hashlib
from pathlib import Path

from fmr_core.crypto import generate_keypair
from fmr_core.d1_client import D1Client
from fmr_core.schema_migrations import ensure_schema
from fmr_tg_scanner.evidence_store import EvidenceStore
from fmr_tg_scanner.explicit_publisher import (
    compute_resource_key,
    enqueue_publish_operation,
    publish_explicit_outbox,
)
from fmr_tg_scanner.search_policy import analyse


def _make_item(msg_id: int, version: str = "v22.1"):
    text = f"#STABLE #ROM #ALIOTH\nLineageOS {version} Unofficial | Android 15\nBy @alice_dev"
    msg = {
        "text": text,
        "channel_username": "testupdates",
        "msg_id": msg_id,
        "tg_post_url": f"https://t.me/testupdates/{msg_id}",
        "published_timestamp": 1793610000,
        "links": [],
        "link_items": [],
    }
    res, _ = analyse(msg, "alioth", ["POCO F3"], {"alioth": ["POCO F3"]})
    assert res is not None
    res["package"]["build_version_tag"] = version
    res["package"]["taxonomy"]["versions"]["project_version"] = version.lstrip("v")
    return {"package": res["package"], "evidence": res["evidence"]}


def test_explicit_add_update_withdraw_preserves_unrelated_rows(tmp_path):
    db = D1Client(local_sqlite_path=str(tmp_path / "d1.sqlite"))
    db.execute_script((Path(__file__).parents[1] / "schema.sql").read_text())
    ensure_schema(db, allow_migration=True)
    db.execute("INSERT INTO brands(id, name, bl_unlock_status) VALUES ('xiaomi', 'Xiaomi', 'easy')")
    db.execute("INSERT INTO devices(codename, brand_id, marketing_name) VALUES ('alioth', 'xiaomi', 'POCO F3')")
    # Pre-existing unrelated custom resource that MUST NEVER be deleted by diffing
    db.execute(
        "INSERT INTO custom_resources(public_token, device_codename, zone_type, category_tag, project_name, target_post_url, ed25519_signature, metadata_json) "
        "VALUES ('cr_legacykeep1', 'alioth', 'free_rom', 'ROM', 'LegacyKeep', 'https://t.me/other/99', 'sig', '{\"audit_version\":\"tg-full-evidence-audit-v1\"}')"
    )

    priv, _ = generate_keypair()
    store = EvidenceStore(tmp_path / "ev.sqlite")
    try:
        item1 = _make_item(10, "v22.1")
        rkey = compute_resource_key("alioth", item1["package"]["tg_post_url"])
        enq1 = enqueue_publish_operation(
            store,
            op_type="ADD",
            resource_key=rkey,
            revision_id="rev-1",
            device_codename="alioth",
            tg_post_url=item1["package"]["tg_post_url"],
            item=item1,
        )
        assert enq1["enqueued"] is True and enq1["op_type"] == "ADD"

        res1 = publish_explicit_outbox(store, db, priv)
        assert res1["added"] == 1 and res1["withdrawn"] == 0
        assert db.query("SELECT COUNT(*) n FROM custom_resources")[0]["n"] == 2

        # UPDATE with new revision_id
        item2 = _make_item(10, "v22.2")
        enq2 = enqueue_publish_operation(
            store,
            op_type="UPDATE",
            resource_key=rkey,
            revision_id="rev-2",
            device_codename="alioth",
            tg_post_url=item2["package"]["tg_post_url"],
            item=item2,
        )
        assert enq2["enqueued"] is True and enq2["op_type"] == "UPDATE"
        res2 = publish_explicit_outbox(store, db, priv)
        assert res2["updated"] == 1
        assert db.query("SELECT build_version_tag FROM custom_resources WHERE public_token=?", (enq1["public_token"],))[0]["build_version_tag"] == "v22.2"

        # Explicit WITHDRAW only deletes the targeted resource_key, leaving cr_legacykeep1 intact
        enq3 = enqueue_publish_operation(
            store,
            op_type="WITHDRAW",
            resource_key=rkey,
            revision_id="rev-3",
            device_codename="alioth",
            tg_post_url=item2["package"]["tg_post_url"],
            withdraw_reason="author_withdrew_build",
        )
        assert enq3["enqueued"] is True
        res3 = publish_explicit_outbox(store, db, priv)
        assert res3["withdrawn"] == 1
        remaining = [r["public_token"] for r in db.query("SELECT public_token FROM custom_resources")]
        assert remaining == ["cr_legacykeep1"]
    finally:
        store.close()
        db.close()
