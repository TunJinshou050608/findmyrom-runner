"""Independent verifier checks, including its own known false-positive hazards."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "full_audit", Path(__file__).parents[1] / "fmr_tg_scanner/quality_full_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class IndependentAuditTests(unittest.TestCase):
    def test_clang_is_not_release_version(self):
        state, evidence = audit.version_role("Build took 5 min | clang version 18.0.0", "", "18.0.0")
        self.assertEqual(state, "contradiction")
        self.assertIn("toolchain_or_driver", evidence["roles"])

    def test_download_size_is_not_version(self):
        self.assertEqual(audit.version_role("Download: Vanilla [1.09 GB]", "", "1.09")[0], "contradiction")

    def test_recommended_firmware_is_not_version(self):
        self.assertEqual(audit.version_role("Recommended firmware V14.0.2.0", "", "V14.0.2.0")[0], "contradiction")

    def test_kernel_filename_over_generic_build_log(self):
        self.assertEqual(audit.independent_kind("Build took 5 min for alioth", "example-alioth-KERNEL-AOSP.zip")[0], "kernel")

    def test_flash_via_recovery_is_not_itself_recovery(self):
        self.assertEqual(audit.independent_kind("Flash via recovery", "brightness-fix-twrp.zip")[0], "patch")

    def test_donor_does_not_imply_compatibility(self):
        state, _, _ = audit.EvidenceAuditor().device_roles(
            "ExampleOS for POCOPHONE F1\nPorted from Redmi K40", "", "")
        self.assertEqual(state, "contradiction")

    def test_explicit_k40_and_other_model_can_coexist(self):
        state, _, _ = audit.EvidenceAuditor().device_roles(
            "ExampleOS | Alioth\nFor aliothin should be OK", "", "")
        self.assertEqual(state, "supported")

    def test_missing_evidence_is_not_a_pass(self):
        self.assertEqual(audit.EvidenceAuditor().device_roles("ExampleOS update", "", "")[0], "unknown")

    def test_longest_model_alias(self):
        a = audit.EvidenceAuditor({"base": ["Example Pad 8"], "pro": ["Example Pad 8 Pro"]})
        self.assertEqual(a.devices("Example Pad 8 Pro"), {"pro"})

    def test_port_from_is_donor_too(self):
        a = audit.EvidenceAuditor({"marble": ["POCO F5"]})
        state, reason, _ = a.device_roles("*Port from marble.", "", "POCO F3 / Redmi K40 updates")
        self.assertEqual(state, "supported")
        self.assertIn("Single-device profile", reason)

    def test_project_name_collision_does_not_declare_device(self):
        a = audit.EvidenceAuditor({"clover": ["Mi Pad 4"]})
        self.assertEqual(a.devices("Clover Project v2.0"), set())

    def test_question_mark_emoji_is_uncertainty(self):
        state, reason, _ = audit.EvidenceAuditor().device_roles("apollo ✅\nalioth ❓", "", "")
        self.assertEqual(state, "unknown")
        self.assertIn("uncertain", reason)


if __name__ == "__main__":
    unittest.main()
