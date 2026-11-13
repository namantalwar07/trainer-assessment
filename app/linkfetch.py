"""Download a recording from a OneDrive or SharePoint 'anyone with the link' share.

Only Microsoft file-sharing hosts are contacted, including after every redirect, so a pasted link can never make the
server fetch an arbitrary address. The server cannot sign in: private ('specific people') links are refused with a
plain explanation.
"""

from __future__ import annotations

import base64
import re
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

ALLOWED = ("sharepoint.com", "onedrive.live.com", "1drv.ms", "1drv.com", "onedrive.com",
           "microsoftpersonalcontent.com", "live.com")
_UA = "Mozilla/5.0 (compatible; TrainerAssessment/1.0)"
_FALLBACK = "onedrive_recording.mp4"
NEEDS_SHARING = ("This link needs a sign-in. In OneDrive choose Share, set it to 'Anyone with the link can view', "
                 "then copy that link.")


class LinkError(ValueError):
    pass


def _host_ok(host: str, allowed: tuple[str, ...]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in allowed)


class _Guard(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed: tuple[str, ...], https_only: bool):
        self.allowed, self.https_only = allowed, https_only

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        p = urllib.parse.urlparse(newurl)
        if (self.https_only and p.scheme != "https") or not _host_ok(p.hostname or "", self.allowed):
            raise LinkError("The link redirects somewhere other than OneDrive or SharePoint, so it was not followed.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _direct_url(url: str, opener, allowed: tuple[str, ...]) -> str:
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if host == "1drv.ms" or host.endswith(".1drv.ms"):  # short link: find where it points
        try:
            with opener.open(urllib.request.Request(url, headers={"User-Agent": _UA}), timeout=30) as r:
                url = r.geturl()
        except urllib.error.HTTPError as e:
            raise LinkError(NEEDS_SHARING if e.code in (401, 403) else f"The link could not be opened (HTTP {e.code}).") from e
        p = urllib.parse.urlparse(url)
        host = (p.hostname or "").lower()
    if host.endswith("sharepoint.com"):
        q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        q = [(k, v) for k, v in q if k.lower() != "download"] + [("download", "1")]
        return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q)))
    if host in ("onedrive.live.com", "1drv.ms") or host.endswith(".onedrive.live.com") or host.endswith("live.com"):
        enc = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        return f"https://api.onedrive.com/v1.0/shares/u!{enc}/root/content"
    return url


def _filename(headers: Message, url: str, ctype: str) -> str:
    name = ""
    cd = headers.get("Content-Disposition") or ""
    if m := re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd, re.I):
        name = urllib.parse.unquote(m.group(1).strip().strip('"'))
    elif m := re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I):
        name = m.group(1).strip()
    if not name:
        name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).replace("..", "_").strip(" .")[:200]
    if not name or name.startswith("_"):
        name = _FALLBACK
    if not re.search(r"\.(mp4|mov)$", name, re.I):
        name += ".mov" if "quicktime" in ctype else ".mp4"
    return name


def open_link(url: str, *, allowed: tuple[str, ...] = ALLOWED, https_only: bool = True, timeout: int = 60):
    """Return (readable response, file name). The caller reads and closes it."""
    url = (url or "").strip()
    p = urllib.parse.urlparse(url)
    if (https_only and p.scheme != "https") or p.scheme not in ("http", "https") or not p.hostname:
        raise LinkError("Paste the full https link that OneDrive or SharePoint gave you.")
    if not _host_ok(p.hostname, allowed):
        raise LinkError("Only OneDrive and SharePoint links are accepted.")
    opener = urllib.request.build_opener(_Guard(allowed, https_only))
    target = _direct_url(url, opener, allowed)
    try:
        resp = opener.open(urllib.request.Request(target, headers={"User-Agent": _UA}), timeout=timeout)
    except urllib.error.HTTPError as e:
        raise LinkError(NEEDS_SHARING if e.code in (401, 403) else
                        "That link was not found. Check it is the right link and has not been deleted."
                        if e.code == 404 else f"The link could not be downloaded (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        raise LinkError("Could not reach the link. Try again in a minute.") from e
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ctype.startswith("text/html") or "login.microsoftonline.com" in resp.geturl():
        resp.close()
        raise LinkError(NEEDS_SHARING)
    return resp, _filename(resp.headers, resp.geturl(), ctype)
