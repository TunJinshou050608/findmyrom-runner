"""Resolve an exact public Telegram floor, never a search snippet or homepage."""
from .html_parser import parse_tg_channel_page,channel_name,ChannelUnavailableError


class MessageIdentityError(ChannelUnavailableError):pass


def read_message(fetch,channel,msg_id):
    channel=channel_name(channel)
    if type(msg_id) is not int or not 0<msg_id<2**53:raise ValueError('Invalid message ID')
    url=f'https://t.me/{channel}/{msg_id}?embed=1&mode=tme'
    html=fetch(url)
    # Embeds lack the channel history wrapper; the message's own data-post
    # identity must still match exactly. Wrapping never manufactures a floor.
    page=parse_tg_channel_page('<section class="tgme_widget_history">'+html+'</section>',channel)
    matches=[m for m in page['messages'] if m['msg_id']==msg_id]
    if len(matches)!=1:raise MessageIdentityError('Requested message absent from embed')
    return matches[0],url
