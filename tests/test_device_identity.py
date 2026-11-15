import pytest
from fmr_core.publish_spec import DeviceSpec
from fmr_ingest_stock.device_identity import clean_model_name, marketing_name


@pytest.mark.parametrize('vendor,name,models,expected', [
    ('asus', 'ROG 7 AI2205', '', 'ROG Phone 7'),
    ('google', '"akita" for Pixel 8a', '', 'Google Pixel 8a'),
    ('samsung', 'Galaxy S23 SM-S911B 32 files · Oct 2026', 'SM-S911B', 'Samsung Galaxy S23'),
    ('oneplus', 'OnePlus 12 PJD110, CPH2581, CPH2573 45 files · Oct 2026', 'PJD110, CPH2581, CPH2573', 'OnePlus 12'),
    ('oppo', 'OPPO Find N5 PKH110, PKH120, CPH2671 22 files · Oct 2026', 'PKH110, PKH120, CPH2671', 'OPPO Find N5'),
    ('asus', 'ROG Phone 6 AI2201 1 file · Aug 2024', 'AI2201', 'ROG Phone 6'),
    ('sony', 'Xperia 10 II (A001SO)', 'A001SO', 'Sony Xperia 10 II'),
    ('nothing', 'Phone (1) spacewar 87 files · Jun 2026', 'spacewar', 'Nothing Phone (1)'),
    ('xiaomi', 'Xiaomi 13 Ultra China', '', 'Xiaomi 13 Ultra'),
    ('xiaomi', 'Redmi Note 12 Pro Speed Edition Global', '', 'Redmi Note 12 Pro Speed Edition'),
    ('oppo', 'OPPO Find X8 CPH2651 (China)', '', 'OPPO Find X8'),
    ('xiaomi', 'Redmi Note 13 Pro+ 5G NFC', '', 'Redmi Note 13 Pro+ 5G NFC'),
    ('google', 'Galaxy Nexus "maguro" (GSM/HSPA+) (with Google Wallet)', '', 'Galaxy Nexus (GSM/HSPA+)'),
    ('vivo', 'Vivo X100 Pro PD2324 China', '', 'vivo X100 Pro'),
    ('realme', 'Realme GT 6 RMX3851 EU', '', 'realme GT 6'),
    ('motorola', 'Moto G54 5G India', '', 'Motorola Moto G54 5G'),
    ('asus', 'Zenfone 9 AI2202 Global', '', 'ASUS Zenfone 9'),
])
def test_source_statistics_and_model_codes_are_not_product_names(vendor, name, models, expected):
    assert marketing_name({'vendor': vendor, 'name': name, 'models': models, 'upstream_id': 'unused'}) == expected


def test_clean_model_name_and_motorola_razr_override():
    assert clean_model_name('Xiaomi 14 Pro (China ROM)') == 'Xiaomi 14 Pro'
    assert clean_model_name('POCO F3 EEA') == 'POCO F3'
    assert marketing_name({'vendor': 'motorola', 'upstream_id': 'aito25', 'name': 'aito25'}) == 'Motorola Razr 60 / Razr 2025'


@pytest.mark.parametrize('restricted_brand', ['huawei', 'honor'])
def test_restricted_brands_are_rejected_at_publish_spec_contract(restricted_brand):
    with pytest.raises(ValueError, match='Restricted brand_id'):
        DeviceSpec(codename='sample', brand_id=restricted_brand, marketing_name='Sample')
