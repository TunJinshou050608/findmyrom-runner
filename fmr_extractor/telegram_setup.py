"""Register SP06 webhook using environment secrets; never send a chat message."""
import json
import os
import re
import sys

import httpx
from .jumper_sync import public_site


def configure(client, token, secret, site):
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise ValueError('Invalid bot token')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', secret):
        raise ValueError('Invalid webhook secret')
    origin = public_site(site)
    base = 'https://api.telegram.org/bot' + token
    for method, payload in [
        ('setWebhook', {'url': origin + '/api/tg-webhook', 'secret_token': secret,
                        'allowed_updates': ['message', 'inline_query'], 'drop_pending_updates': False}),
        ('setMyCommands', {'commands': [
            {'command': 'boot', 'description': 'Find boot images by device'},
            {'command': 'rom', 'description': 'Find official firmware by device'},
        ]}),
    ]:
        response = client.post(base + '/' + method, json=payload)
        response.raise_for_status()
        if response.json().get('ok') is not True:
            raise RuntimeError('Telegram configuration rejected')
    return {'configured': True, 'webhook_url': origin + '/api/tg-webhook'}


def main():
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            result = configure(client, os.environ['TG_BOT_TOKEN'], os.environ['TG_WEBHOOK_SECRET'],
                               os.environ.get('PUBLIC_SITE_URL', 'https://findmyrom.com'))
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        # Telegram API URLs contain the bot token; never echo an exception URL.
        print(f'Telegram setup failed ({type(exc).__name__})', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
