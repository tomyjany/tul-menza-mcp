"""Headless TUL Shibboleth/Microsoft SAML sign-in.

Interactive steps intentionally require a browser. Never send credentials to a host
supplied by an untrusted page without checking its exact HTTPS origin first.
"""

from __future__ import annotations

import html
import json
import re
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://menza.tul.cz"
LOGIN_URL = BASE_URL + "/Shibboleth.sso/Login?target=" + BASE_URL + "/"
MS_BASE = "https://login.microsoftonline.com"
_ALLOWED_HOSTS = {"menza.tul.cz", "shibbo.tul.cz", "login.microsoftonline.com"}

_INTERACTIVE_PGIDS = {
    "ConvergedTFA": "multi-factor authentication",
    "ConvergedProofUpRedirect": "security info registration (MFA setup)",
    "ConvergedChangePassword": "password change",
    "ConvergedConsent": "app consent",
    "BssoInterrupt": "device sign-in interrupt",
    "CmsiInterrupt": "sign-in interrupt",
}


class AuthError(RuntimeError):
    pass


def _safe_url(url: str, *, hosts: set[str] = _ALLOWED_HOSTS) -> str:
    """Return URL only for a literal approved HTTPS origin (never a suffix match)."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in hosts or
                parsed.port not in (None, 443) or parsed.username or parsed.password or
                "\\" in url or any(ord(c) < 32 for c in url)):
            raise ValueError
    except ValueError:
        raise AuthError("sign-in attempted to leave the approved HTTPS hosts") from None
    return url


def _ms_config(text: str) -> dict | None:
    m = re.search(r"\$Config=(\{.*?\});\s*//\]\]>", text, re.S)
    if not m:
        return None
    try:
        config = json.loads(m.group(1))
        return config if isinstance(config, dict) else None
    except ValueError:
        return None


def _hidden_form(text: str, base: str) -> tuple[str, dict] | None:
    soup = BeautifulSoup(text, "html.parser")
    for form in soup.find_all("form"):
        if not form.get("action"):
            continue
        fields = {i["name"]: i.get("value", "") for i in form.find_all("input") if i.get("name")}
        if "SAMLResponse" in fields or "SAMLRequest" in fields or any(k.startswith("shib_idp_ls") for k in fields):
            return urljoin(base, html.unescape(form["action"])), fields
    return None


def _request(session: requests.Session, method: str, url: str, *, timeout: float, **kwargs):
    """Follow safe GET redirects ourselves; never forward POST bodies on a redirect."""
    for _ in range(12):
        _safe_url(url)
        try:
            response = session.request(method, url, timeout=timeout, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise AuthError("sign-in network request failed") from None
        if response.is_redirect:
            location = response.headers.get("Location")
            if not location:
                raise AuthError("sign-in redirect has no destination")
            target = _safe_url(urljoin(response.url, location))
            if method == "POST" and response.status_code in (307, 308):
                raise AuthError("sign-in refused to forward a POST body on redirect")
            url, method, kwargs = target, "GET", {}
            continue
        if response.status_code >= 400:
            raise AuthError("sign-in server rejected a request")
        _safe_url(response.url)
        return response
    raise AuthError("too many sign-in redirects")


def login(session: requests.Session, username: str, password: str, *, timeout: float = 30.0) -> None:
    """Sign into menza.tul.cz; reject unknown identity steps without leaking secrets."""
    if not username or not password:
        raise AuthError("MENZA_USERNAME and MENZA_PASSWORD must be set")

    r = _request(session, "GET", LOGIN_URL, timeout=timeout)
    credential_posted = False
    for _ in range(12):
        if urlsplit(_safe_url(r.url)).hostname == "menza.tul.cz" and 'href="/odhlasit/"' in r.text:
            return
        cfg = _ms_config(r.text)
        if cfg is not None:
            if urlsplit(r.url).hostname != "login.microsoftonline.com":
                raise AuthError("Microsoft form appeared on an unexpected host")
            pgid = cfg.get("pgid", "")
            # 50058 on the initial page means no existing SSO session, not a
            # rejected password. Only password-submission responses establish
            # credential failure; initial interactive challenges still stop below.
            if credential_posted and (cfg.get("sErrorCode") or cfg.get("sErrTxt")):
                raise AuthError("Microsoft rejected the sign-in; check the password")
            if cfg.get("fShowHipChallenge") or cfg.get("sHipFid"):
                raise AuthError("Microsoft requires a captcha; sign in in a browser")
            if pgid in _INTERACTIVE_PGIDS:
                raise AuthError(f"sign-in needs {_INTERACTIVE_PGIDS[pgid]}; use a browser")
            if pgid == "KmsiInterrupt":
                action = _safe_url(urljoin(MS_BASE, cfg.get("urlPost", "/kmsi")), hosts={"login.microsoftonline.com"})
                r = _request(session, "POST", action, timeout=timeout, data={
                    "LoginOptions": "1", "type": "28", "ctx": cfg["sCtx"],
                    "hpgrequestid": cfg.get("sessionId", ""), "flowToken": cfg["sFT"],
                    "canary": cfg["canary"], "i19": "1000",
                })
                continue
            if credential_posted:
                raise AuthError("unsupported Microsoft sign-in step")
            try:
                credential_url = _safe_url(urljoin(MS_BASE, cfg["urlGetCredentialType"]), hosts={"login.microsoftonline.com"})
                password_url = _safe_url(urljoin(MS_BASE, cfg["urlPost"]), hosts={"login.microsoftonline.com"})
                ctx, flow, canary = cfg["sCtx"], cfg["sFT"], cfg["canary"]
            except KeyError:
                raise AuthError("incomplete Microsoft login form") from None
            cred_response = _request(session, "POST", credential_url, timeout=timeout, json={
                "username": username, "isOtherIdpSupported": True, "checkPhones": False,
                "isRemoteNGCSupported": True, "isCookieBannerShown": False,
                "isFidoSupported": False, "originalRequest": ctx, "flowToken": flow,
            }, headers={"canary": cfg.get("apiCanary", ""),
                        "client-request-id": cfg.get("correlationId", ""),
                        "hpgrequestid": cfg.get("sessionId", "")})
            try:
                cred = cred_response.json()
                if not isinstance(cred, dict):
                    raise ValueError
            except ValueError:
                raise AuthError("invalid Microsoft credential response") from None
            if cred.get("ThrottleStatus") == 1:
                raise AuthError("Microsoft is throttling sign-ins")
            if cred.get("IfExistsResult") not in (0, 5, 6):
                raise AuthError("Microsoft does not recognise this TUL account")
            if cred.get("Credentials", {}).get("FederationRedirectUrl"):
                raise AuthError("account is federated to another identity provider")
            credential_posted = True
            r = _request(session, "POST", password_url, timeout=timeout, data={
                "i13": "0", "login": username, "loginfmt": username, "type": "11",
                "LoginOptions": "3", "lrt": "", "lrtPartition": "", "hisRegion": "",
                "hisScaleUnit": "", "passwd": password, "ps": "2", "psRNGCDefaultType": "",
                "psRNGCEntropy": "", "psRNGCSLK": "", "canary": canary, "ctx": ctx,
                "hpgrequestid": cfg.get("sessionId", ""), "flowToken": cred.get("FlowToken") or flow,
                "PPSX": "", "NewUser": "1", "FoundMSAs": "", "fspost": "0",
                "i21": "0", "CookieDisclosure": "0", "IsFidoSupported": "0",
                "isSignupPost": "0", "isRecoveryAttemptPost": "0", "i19": "9000",
            })
            continue
        form = _hidden_form(r.text, r.url)
        if form is None:
            raise AuthError("unknown or interactive sign-in page; use a browser")
        action, fields = form
        target = urlsplit(_safe_url(action, hosts={"shibbo.tul.cz", "menza.tul.cz"}))
        source = urlsplit(r.url).hostname
        if "SAMLResponse" in fields:
            expected = {
                "login.microsoftonline.com": ("shibbo.tul.cz", "/idp/profile/Authn/SAML2/POST/SSO"),
                "shibbo.tul.cz": ("menza.tul.cz", "/Shibboleth.sso/SAML2/POST"),
            }.get(source)
            if expected != (target.hostname, target.path):
                raise AuthError("unexpected SAML response destination")
        elif "SAMLRequest" in fields:
            if source != "menza.tul.cz" or target.hostname != "shibbo.tul.cz":
                raise AuthError("unexpected SAML request destination")
        elif "shib_idp_ls_supported" in fields and source == "shibbo.tul.cz" and target.hostname == "shibbo.tul.cz":
            fields["shib_idp_ls_supported"] = ""
            fields.update({k: "false" for k in fields if k.startswith("shib_idp_ls_success")})
            fields.setdefault("_eventId_proceed", "")
        else:
            raise AuthError("unexpected identity form during sign-in")
        r = _request(session, "POST", action, timeout=timeout, data=fields)
    raise AuthError("sign-in did not reach a logged-in menza page")
