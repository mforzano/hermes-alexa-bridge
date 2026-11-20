"""Speech-only rendering and pagination; never rewrite a Hermes result."""
import re
from dataclasses import dataclass
from unicodedata import category
from urllib.parse import urlsplit

MAX_SPEECH = 600
PAGE_TTL = 300
MORE_PROMPT = ' Say read more to hear the next part.'
NO_MORE = 'There’s no more of the last answer to read here.'


@dataclass(frozen=True)
class SpeechReply:
    text: str


_SENTENCE = re.compile(r'''[.!?]+["'”’\])]*(?= |$)''')
_ABBREVIATION = re.compile(r'(?:\b(?:mr|mrs|ms|dr|st|ave|rd|vs|etc|e\.g|i\.e)|\b[A-Z])\.$', re.I)
_ENTITY = re.compile(r'&(?=(?:#[0-9]+|#x[0-9a-f]+|[a-z][a-z0-9]*);)', re.I)
_GOOGLE = r'google\.(?:com|(?:co|com)\.[a-z]{2}|[a-z]{2})'
_MAP_HOST = rf'(?:(?:www\.|maps\.)?{_GOOGLE}|maps\.app\.goo\.gl|goo\.gl)'
_URL_TAIL = r'(?:[^\s<>()\[\]`]|\([^\s<>()]*\))'
_LINK = re.compile(rf'''\[([^\[\]\r\n]*)\]\([ \t]*(<[^<>\s]+>|{_URL_TAIL}+?)(?:[ \t]+(?:"[^"\r\n]*"|'[^'\r\n]*'))?[ \t]*\)''')
_URL = rf'(?:https?://{_URL_TAIL}+|{_MAP_HOST}(?=[/?#]){_URL_TAIL}*)'
_BARE = re.compile(rf'(?<![\w./@-])(?:<(?P<angle>{_URL})>|\((?P<paren>{_URL})\)|`(?P<code>{_URL})`|(?P<url>{_URL}))', re.I)


def _is_google_maps(url):
    try:
        parsed = urlsplit(url if '://' in url else '//' + url)
        host = parsed.hostname or ''
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    path = parsed.path.casefold().split('/')
    if host == 'maps.app.goo.gl':
        return True
    if host == 'goo.gl':
        return path[1:2] == ['maps']
    if re.fullmatch(rf'maps\.{_GOOGLE}', host):
        return True
    return bool(re.fullmatch(rf'(?:www\.)?{_GOOGLE}', host) and path[1:2] == ['maps'])


def _omit_maps(text):
    def link(match):
        label, destination = match.groups()
        return label if _is_google_maps(destination.strip('<>')) else match[0]

    def bare(match):
        url = match[match.lastgroup]
        core = url.rstrip('.,;:!?') if match.lastgroup == 'url' else url
        return ' ' + url[len(core):] if _is_google_maps(core) else match[0]

    return _BARE.sub(bare, _LINK.sub(link, text))


def plain_speech(text, *, omit_google_maps_urls=False):
    filtered = _omit_maps(text) if omit_google_maps_urls else text
    maps_removed = filtered != text
    # Keep ordinary URL query separators intact without sending SSML markup.
    text = _ENTITY.sub('%26', filtered)
    text = ' '.join(''.join(' ' if c in '<>' or category(c).startswith('C') else c for c in text).split())
    if maps_removed:
        text = re.sub(r' +([.,;:!?])', r'\1', text)
        if not any(c.isalnum() for c in text):
            return 'There’s no text to read after omitting map links.'
    return text


def take_page(text, *, offer_more=True):
    """Consume one page of normalized speech, cutting only between tokens."""
    if len(text) <= MAX_SPEECH:
        return text, ''
    budget = MAX_SPEECH - (len(MORE_PROMPT) if offer_more else 0)
    # One extra character lets a sentence at exactly the limit see its space,
    # while not treating the page budget itself as a sentence terminator.
    end = max((m.end() for m in _SENTENCE.finditer(text, 0, budget + 1)
               if m.end() <= budget and not _ABBREVIATION.search(text, 0, m.end())), default=0)
    if not end:
        end = text.rfind(' ', 0, budget + 1)
    if end > 0:
        page, rest = text[:end], text[end:].lstrip()
    else:
        end = text.find(' ')
        page, rest = 'A long item was omitted.', text[end + 1:] if end >= 0 else ''
    return page + (MORE_PROMPT if rest and offer_more else ''), rest
