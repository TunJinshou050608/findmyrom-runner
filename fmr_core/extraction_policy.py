"""Only complete, static ZIP sources can enter the cloud Range extractor."""

from urllib.parse import urlsplit

EXTRACTION_BRANDS = frozenset({
    'xiaomi', 'google', 'oneplus', 'motorola', 'nothing',
    'asus', 'rog', 'oppo', 'vivo', 'realme',
})
EXTRACTION_PACKAGES = frozenset({'recovery_zip', 'factory_zip', 'fastboot_zip'})


def extraction_eligible(brand, package, url):
    if brand not in EXTRACTION_BRANDS or package not in EXTRACTION_PACKAGES:
        return False
    source = urlsplit(url)
    return (source.scheme == 'https' and bool(source.hostname)
            and source.hostname != 'findmyrom.com'
            and source.path.lower().endswith('.zip')
            and not source.query and not source.fragment
            and not source.username and not source.password)
