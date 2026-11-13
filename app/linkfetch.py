"""Download a recording from a OneDrive or SharePoint 'anyone with the link' share.

Only Microsoft file-sharing hosts are contacted, including after every redirect, so a pasted link can never make the
server fetch an arbitrary address. The server cannot sign in: private ('specific people') links are refused with a
plain explanation.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
ALLOWED = ("sharepoint.com", "onedrive.live.com", "1drv.ms", "1drv.com", "onedrive.com",
           "microsoftpersonalcontent.com", "live.com")
_UA = "Mozilla/5.0 (compatible; TrainerAssessment/1.0)"
_FALLBACK = "onedrive_recording.mp4"
VIDEO_RE = re.compile(r"\.(mp4|mov)$", re.I)
NO_ACCESS = ("The server's company account cannot open this file. Ask the admin to give the Trainer Assessment "
             "app access to the folder that holds it, or share the file with 'Anyone with the link'.")
NEEDS_SHARING = ("This link needs a sign-in. In OneDrive choose Share, set it to 'Anyone with the link can view', "
                 "then copy that link.")


class LinkError(ValueError):
    pass


class GraphAccess:
    """Optional company sign-in (Microsoft Graph, app-only) so private SharePoint/OneDrive links can be read.

    The app can read only what the tenant admin granted it (ideally one site via Sites.Selected). Secrets come from
    the environment and are never logged.
    """

    def __init__(self, tenant: str, client_id: str, secret: str, *, base: str = GRAPH_BASE, token_url: str = ""):
        self.tenant, self.client_id, self.secret = tenant, client_id, secret
        self.base = base.rstrip("/")
        self.token_url = token_url or TOKEN_URL.format(tenant=urllib.parse.quote(tenant))

    def _token(self) -> str:
        body = urllib.parse.urlencode({"client_id": self.client_id, "client_secret": self.secret,
                                       "grant_type": "client_credentials",
                                       "scope": "https://graph.microsoft.com/.default"}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.token_url, data=body), timeout=30) as r:
                return json.load(r)["access_token"]
        except (urllib.error.URLError, KeyError, ValueError) as e:
            raise LinkError("The company sign-in for OneDrive is not working. Ask the admin to check the "
                            "Microsoft app settings on the server.") from e

    def _get(self, path_or_url: str) -> dict:
        full = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        if not full.startswith(self.base + "/"):
            raise LinkError("OneDrive returned an unexpected address, so it was not followed.")
        req = urllib.request.Request(full, headers={"Authorization": f"Bearer {self._token()}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise LinkError(NO_ACCESS) from e
            raise LinkError(f"OneDrive refused the request (HTTP {e.code}).") from e
        except (urllib.error.URLError, ValueError) as e:
            raise LinkError("Could not reach OneDrive. Try again in a minute.") from e

    @staticmethod
    def _entry(d: dict) -> dict:
        return {"name": d.get("name") or _FALLBACK, "size": int(d.get("size") or 0),
                "drive": (d.get("parentReference") or {}).get("driveId", ""), "id": d.get("id", ""),
                "dl": d.get("@microsoft.graph.downloadUrl", "")}

    def resolve(self, url: str) -> dict:
        """A share link is either one file ({'kind': 'file', ...}) or a folder ({'kind': 'folder', 'files': [...]})."""
        enc = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        d = self._get(f"/shares/u!{enc}/driveItem")
        if "folder" not in d:
            e = self._entry(d)
            if not e["dl"]:
                raise LinkError("That link has no downloadable file. Paste the link to the video itself.")
            return {"kind": "file", **e}
        files, nxt = [], f"/shares/u!{enc}/driveItem/children?$top=200"
        while nxt and len(files) < 1000:
            page = self._get(nxt)
            files += [self._entry(c) for c in page.get("value", []) if "file" in c and VIDEO_RE.search(c.get("name", ""))]
            nxt = page.get("@odata.nextLink")
        return {"kind": "folder", "files": sorted(files, key=lambda f: f["name"].lower())}

    def by_id(self, drive: str, item: str) -> tuple[str, str]:
        q = urllib.parse.quote
        e = self._entry(self._get(f"/drives/{q(drive, safe='')}/items/{q(item, safe='')}"))
        if not e["dl"]:
            raise LinkError("That file has no downloadable content.")
        return e["name"], e["dl"]


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


def _download(dl: str, name: str, opener, https_only: bool, allowed: tuple[str, ...], timeout: int):
    dp = urllib.parse.urlparse(dl)
    if (https_only and dp.scheme != "https") or not _host_ok(dp.hostname or "", allowed):
        raise LinkError("OneDrive returned an unexpected download address, so it was not followed.")
    try:
        resp = opener.open(urllib.request.Request(dl, headers={"User-Agent": _UA}), timeout=timeout)
    except urllib.error.HTTPError as e:
        raise LinkError(f"The file could not be downloaded (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        raise LinkError("Could not reach the link. Try again in a minute.") from e
    return resp, _filename(Message(), "/" + name, (resp.headers.get("Content-Type") or "").lower())


def open_item(graph: GraphAccess, drive: str, item: str, *, allowed: tuple[str, ...] = ALLOWED,
              https_only: bool = True, timeout: int = 60):
    """Open one file of a folder the user already listed (the web layer checks the signed token first)."""
    name, dl = graph.by_id(drive, item)
    return _download(dl, name, urllib.request.build_opener(_Guard(allowed, https_only)), https_only, allowed, timeout)


def open_link(url: str, *, allowed: tuple[str, ...] = ALLOWED, https_only: bool = True, timeout: int = 60,
              graph: GraphAccess | None = None):
    """Return (readable response, file name). The caller reads and closes it."""
    url = (url or "").strip()
    p = urllib.parse.urlparse(url)
    if (https_only and p.scheme != "https") or p.scheme not in ("http", "https") or not p.hostname:
        raise LinkError("Paste the full https link that OneDrive or SharePoint gave you.")
    if not _host_ok(p.hostname, allowed):
        raise LinkError("Only OneDrive and SharePoint links are accepted.")
    opener = urllib.request.build_opener(_Guard(allowed, https_only))
    had_graph = graph is not None
    if graph is not None:  # private files: ask Microsoft for a short-lived download address using the company account
        try:
            found = graph.resolve(url)
        except LinkError as e:
            if str(e) != NO_ACCESS:
                raise
            graph = None  # not reachable by the company account: try it as a public 'anyone with the link' share
        else:
            if found["kind"] == "folder":
                raise LinkError("That link is a folder. Use 'Add from links' on the upload page, which lists its videos.")
            return _download(found["dl"], found["name"], opener, https_only, allowed, timeout)
    target = _direct_url(url, opener, allowed)
    try:
        resp = opener.open(urllib.request.Request(target, headers={"User-Agent": _UA}), timeout=timeout)
    except urllib.error.HTTPError as e:
        raise LinkError((NO_ACCESS if had_graph else NEEDS_SHARING) if e.code in (401, 403) or (had_graph and e.code == 404) else
                        "That link was not found. Check it is the right link and has not been deleted."
                        if e.code == 404 else f"The link could not be downloaded (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        raise LinkError("Could not reach the link. Try again in a minute.") from e
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ctype.startswith("text/html") or "login.microsoftonline.com" in resp.geturl():
        resp.close()
        raise LinkError(NO_ACCESS if had_graph else NEEDS_SHARING)
    return resp, _filename(resp.headers, resp.geturl(), ctype)
