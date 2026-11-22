"""Tests for config/official_sources.yaml registry loader (Phase 3 Layer 1 & Layer 3)."""
from fmr_tg_scanner.official_registry import load_official_registry


def test_official_registry_layer1_channels_and_domains():
    reg = load_official_registry()
    assert reg.is_layer1_blocked_channel("MIUIUpdatesTracker")[0] is True
    assert reg.is_layer1_blocked_channel("@crdroidupdates")[0] is True
    assert reg.is_layer1_blocked_channel("orangefoxupdates")[0] is True
    assert reg.is_layer1_blocked_channel("random_unofficial_builds")[0] is False

    assert reg.is_oem_stock_domain("https://bigota.d.miui.com/OS1.0/test.zip") is True
    assert reg.is_oem_stock_domain("https://sourceforge.net/projects/crdroid/files/rom.zip") is False
    assert reg.all_external_links_are_oem_stock(["https://t.me/some/1", "https://dl.google.com/dl/aosp/img.zip"]) is True
    assert reg.all_external_links_are_oem_stock(["https://t.me/some/1"]) is False


def test_official_registry_layer3_respects_status_and_archived_projects():
    reg = load_official_registry()
    # Active official projects -> excluded ONLY when release_status == 'official'
    assert reg.should_exclude_official_project("LineageOS", "official", "rom")[0] is True
    assert reg.should_exclude_official_project("Evolution X", "official", "rom")[0] is True
    assert reg.should_exclude_official_project("DerpFest", "official", "rom")[0] is True
    assert reg.should_exclude_official_project("AxionOS", "official", "rom")[0] is True
    assert reg.should_exclude_official_project("TWRP", "official", "recovery")[0] is True
    assert reg.should_exclude_official_project("OrangeFox", "official", "recovery")[0] is True

    # Unofficial / Port -> NEVER excluded
    assert reg.should_exclude_official_project("LineageOS", "unofficial", "rom")[0] is False
    assert reg.should_exclude_official_project("Evolution X", "port", "rom")[0] is False
    assert reg.should_exclude_official_project("HyperOS", "port", "rom")[0] is False

    # Discontinued / Archived -> NEVER excluded even if labeled official
    for archived in ("Project Matrixx", "PixelExperience", "DivestOS"):
        assert reg.should_exclude_official_project(archived, "official", "rom")[0] is False
