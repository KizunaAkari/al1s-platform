import os
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from al1s.infrastructure.direct_http import NoRedirect
from al1s.meme_workshop.images import MAX_BYTES

HOSTS = {
    "q1.qlogo.cn",
    "q2.qlogo.cn",
    "q4.qlogo.cn",
    "q.qlogo.cn",
    "gchat.qpic.cn",
    "c2cpicdw.qpic.cn",
    "multimedia.nt.qq.com.cn",
    "multimedia.nt.qq.com",
}


def permitted_media_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in HOSTS
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def read_media(url: str) -> bytes:
    if not permitted_media_url(url):
        raise ValueError("meme_media_source_rejected")
    proxy = os.environ.get("AL1S_BOT_HTTP_PROXY", "").strip()
    proxies = {"https": proxy} if proxy else {}
    try:
        with build_opener(ProxyHandler(proxies), NoRedirect()).open(
            Request(url, headers={"User-Agent": "AL1S-Meme/1"}),
            timeout=5,
        ) as response:
            body = bytes(response.read(MAX_BYTES + 1))
    except (OSError, TimeoutError, ValueError) as exc:
        raise ValueError("meme_media_unavailable") from exc
    if not body or len(body) > MAX_BYTES:
        raise ValueError("meme_media_size")
    return body
