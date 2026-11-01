import pytest
from fmr_ingest_stock.device_identity import marketing_name
from fmr_ingest_stock import staged_catalogue,vendor_catalogue


@pytest.mark.parametrize('vendor,name,models,expected',[
 ('asus','ROG 7 AI2205','','ROG Phone 7'),
 ('google','"akita" for Pixel 8a','','Google Pixel 8a'),
 ('samsung','Galaxy S23 SM-S911B 32 files · Oct 2026','SM-S911B','Samsung Galaxy S23'),
 ('oneplus','OnePlus 12 PJD110, CPH2581, CPH2573 45 files · Oct 2026','PJD110, CPH2581, CPH2573','OnePlus 12'),
 ('oppo','OPPO Find N5 PKH110, PKH120, CPH2671 22 files · Oct 2026','PKH110, PKH120, CPH2671','OPPO Find N5'),
 ('asus','ROG Phone 6 AI2201 1 file · Aug 2024','AI2201','ROG Phone 6'),
 ('sony','Xperia 10 II (A001SO)','A001SO','Sony Xperia 10 II'),
 ('nothing','Phone (1) spacewar 87 files · Jun 2026','spacewar','Nothing Phone (1)'),
])
def test_source_statistics_and_model_codes_are_not_product_names(vendor,name,models,expected):
 assert marketing_name({'vendor':vendor,'name':name,'models':models,'upstream_id':'unused'})==expected


def test_suppressed_collectors_do_not_even_open_local_or_remote_state(monkeypatch):
 class NoDatabase:
  def __getattr__(self,name):raise AssertionError('Suppressed vendor accessed state')
 monkeypatch.delenv('SIGN_PRIVATE_KEY_HEX',raising=False)
 for vendor in ('huawei','honor'):
  assert staged_catalogue.collect(vendor)==0
  assert vendor_catalogue.run(NoDatabase(),vendor)==0
