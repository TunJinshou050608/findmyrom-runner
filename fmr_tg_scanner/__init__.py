"""TG01 public-preview metadata engine; shares the project's single Telegram bot."""
from .crawler import scan_channel
from .html_parser import parse_tg_channel_page
from .rule_extractor import extract_package_by_rules
from .bridge import export_to_custom_resources
__all__=['scan_channel','parse_tg_channel_page','extract_package_by_rules','export_to_custom_resources']
