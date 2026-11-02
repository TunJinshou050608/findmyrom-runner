"""Synthetic role/conflict cases; never network calls or private source fixtures."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "quality", Path(__file__).parents[1] / "fmr_tg_scanner/quality_filter.py")
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


class QualityTests(unittest.TestCase):
    def row(self, text, attachment=None, mid=1):
        return {"url": f"https://t.me/example_updates/{mid}", "text": text,
                "observed_attachment": attachment, "channel": "example_updates"}

    def review(self, text, attachment=None, target="alioth", catalogue=None):
        return quality.QualityFilter(catalogue).review(self.row(text, attachment), target)

    def test_recipient_over_donor(self):
        r = self.review("ExampleOS Port for POCOPHONE F1\nPorted from Redmi K40")
        self.assertEqual(r["status"], "rejected")

    def test_donor_without_recipient_is_not_compatibility(self):
        r = self.review("ExampleOS | Port\nNotes:\nPorted from Alioth")
        self.assertEqual(r["status"], "rejected")

    def test_primary_rom_not_kernel_credit(self):
        r = self.review("#Example #Alioth\nExampleOS - UNOFFICIAL | Android 13\nBy @builder123\nDownload [1.7 GB]\nCredits:\n@other123 for kernel")
        self.assertEqual((r["status"], r["resource_kind"]), ("accepted", "rom"))
        self.assertIsNone(r["build_version_tag"])

    def test_steps_and_dependencies_not_primary(self):
        r = self.review("AOSP A12 Beta | UNOFFICIAL | Android 12.1\nBy @builder123\nDownload [1.3GB]\nHow to flash:\nReboot to Recovery\nRecommend firmware V14.0.2.0\nFollow @community\n#Alioth")
        self.assertEqual((r["status"], r["resource_kind"]), ("accepted", "rom"))
        self.assertIsNone(r["build_version_tag"])

    def test_new_primary_title_never_legacy_filename(self):
        row = self.row("ExampleOS v2.3 for alioth\nUpdated: 11/04/2025")
        row["title"] = "wrong-device-gapps.zip"
        self.assertEqual(quality.QualityFilter().review(row, "alioth")["title"], "ExampleOS v2.3 for alioth")

    def test_recovery_official_is_not_rom_official(self):
        r = self.review("TWRP v3.7.0 - OFFICIAL | Android 13\nDevice: alioth")
        self.assertEqual((r["status"], r["resource_kind"]), ("accepted", "recovery"))

    def test_explicit_foreign_attachment_over_profile(self):
        row = self.row("Flash on recovery", "orangefox-munch.zip")
        r = quality.QualityFilter().review(row, "alioth", "POCO F3 updates")
        self.assertEqual(r["status"], "rejected")

    def test_mixed_profile_cannot_establish_target(self):
        r = quality.QualityFilter().review(self.row("ExampleOS v2 | Unofficial"), "alioth",
                                          "POCO F3 | POCO F4 | Mi10T/Pro")
        self.assertEqual(r["status"], "candidate")

    def test_profile_model_longest_alias_wins(self):
        engine = quality.QualityFilter({"base": ["Example Pad 8"], "pro": ["Example Pad 8 Pro"]})
        self.assertEqual(engine.devices("Example Pad 8 Pro"), {"pro"})

    def test_foreign_target_without_alias_must_not_be_invented(self):
        r = self.review("ExampleOS for Vega\nPorted from Lyra", target="vega",
                        catalogue={"vega": ["Device 21"], "lyra": ["Device 22"]})
        self.assertEqual(r["status"], "accepted")

    def test_patch_not_recovery(self):
        r = self.review("Brightness fix\nDevice: alioth\nFlash from Recovery", "brightness-fix-twrp.zip")
        self.assertEqual((r["status"], r["resource_kind"]), ("candidate", "patch"))

    def test_unproved_target_is_retained(self):
        r = self.review("ExampleOS v1.1 | Unofficial")
        self.assertEqual(r["status"], "candidate")

    def test_thousand_separator_and_ram(self):
        r = self.review("ExampleOS v1.2 for alioth\nDownload [1,019 MB]\nRequires 8GB RAM")
        self.assertEqual([s["bytes_claimed"] for s in r["sizes"]], [1019000000])

    def test_author_and_publisher_are_separate(self):
        r = self.review("ExampleOS for alioth\nMaintainer: BuilderName")
        self.assertEqual(r["maintainer_display_name"], "BuilderName")
        self.assertIsNone(r["maintainer_handle"])
        self.assertFalse(r["author_identity_verified"])

    def test_equal_checksum_groups_without_losing_sources(self):
        text = "AICP build for alioth\nChecksum: " + "a" * 64
        out = quality.screen_records([self.row(text, mid=1), self.row(text, mid=2)], "alioth")
        self.assertEqual(out["status_counts"], {"accepted": 1, "duplicate": 1})
        self.assertEqual(len(out["resources"][0]["source_posts"]), 2)

    def test_empty_or_template_captions_preserve_different_attachments(self):
        rows = [self.row("AOSP", f"5.10.200-alioth-AOSP-{i}.zip", i) for i in (1, 2)]
        out = quality.screen_records(rows, "alioth")
        self.assertEqual(out["status_counts"], {"accepted": 2})

    def test_shared_dependency_url_is_not_a_dedup_key(self):
        rows = []
        for i in (1, 2):
            row = self.row(f"ExampleOS v2.{i} for alioth\nUpdated: 0{i}/04/2025", mid=i)
            row["observed_links"] = ["https://files.example.org/shared-vendor_boot.img"]
            rows.append(row)
        self.assertEqual(quality.screen_records(rows, "alioth")["status_counts"], {"accepted": 2})

    def test_replay_replaces_old_accepted_without_mutating_input(self):
        row = self.row("ExampleOS for POCO F4\nPorted from alioth")
        row["status"] = "accepted"
        before = dict(row)
        self.assertEqual(quality.screen_records([row], "alioth")["status_counts"], {"rejected": 1})
        self.assertEqual(row, before)

    def test_missing_text_is_candidate_not_accepted(self):
        self.assertEqual(self.review("")["status"], "candidate")

    def test_byte_claim_not_verified_size(self):
        r = self.review("ExampleOS for alioth\nDownload [2 GiB]")
        self.assertEqual(r["sizes"][0]["bytes_claimed"], 2 * 1024**3)
        self.assertFalse(r["sizes"][0]["verified_bytes"])

    def test_emoji_date_is_not_a_kernel_version(self):
        r = self.review("ExampleKernel | Unofficial\n📆 08.01.2025\nDevice: alioth")
        self.assertIsNone(r["build_version_tag"])

    def test_kernel_compatibility_caption_is_not_version(self):
        r = self.review("For ColorOS 16 or OxygenOS 13.1\nUse this kernel\nOnly for alioth",
                        "5.10.200-alioth-build-1.zip")
        self.assertEqual(r["resource_kind"], "kernel")
        self.assertEqual(r["build_version_tag"], "5.10.200")


if __name__ == "__main__":
    unittest.main()
