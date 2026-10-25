import json

import pytest
import requests

from menza_hermes.auth import AuthError, LOGIN_URL, _safe_url, login


USER = "test@example.invalid"
PASS = "synthetic-password-never-log"
MS = "https://login.microsoftonline.com"


def response(url, body="", status=200, location=None):
    r = requests.Response()
    r.url = url
    r.status_code = status
    r._content = body.encode()
    if location:
        r.headers["Location"] = location
    return r


def config(**overrides):
    c = {"urlPost": "/password", "urlGetCredentialType": "/check", "sCtx": "context",
         "sFT": "flow", "canary": "canary", "pgid": "ConvergedSignIn"}
    c.update(overrides)
    return "$Config=" + json.dumps(c) + "; //]]>"


class FakeSession:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = next(self.replies)
        if isinstance(r, Exception):
            raise r
        return r


def test_microsoft_password_only_goes_to_exact_https_host():
    for unsafe in ("https://login.microsoftonline.com.evil.invalid/steal",
                   "http://login.microsoftonline.com/steal",
                   "https://user@login.microsoftonline.com/steal",
                   "https://login.microsoftonline.com:444/steal"):
        session = FakeSession([response(MS + "/login", config(urlPost=unsafe))])
        with pytest.raises(AuthError):
            login(session, USER, PASS)
        assert len(session.calls) == 1
    assert _safe_url(MS + "/password") == MS + "/password"


def test_get_redirect_target_is_checked_before_following():
    session = FakeSession([response(LOGIN_URL, status=302, location="https://evil.invalid/")])
    with pytest.raises(AuthError):
        login(session, USER, PASS)
    assert len(session.calls) == 1


def test_initial_microsoft_no_sso_error_allows_password_sign_in():
    to_idp = ('<form action="https://shibbo.tul.cz/idp/profile/Authn/SAML2/POST/SSO">'
              '<input name="RelayState" value="synthetic-relay" />'
              '<input name="SAMLResponse" value="synthetic-assertion" /></form>')
    to_menza = ('<form action="https://menza.tul.cz/Shibboleth.sso/SAML2/POST">'
                '<input name="RelayState" value="synthetic-relay" />'
                '<input name="SAMLResponse" value="synthetic-assertion" /></form>')
    session = FakeSession([response(MS + "/login", config(sErrorCode="50058")),
                           response(MS + "/check", '{"IfExistsResult":0}'),
                           response(MS + "/password", to_idp),
                           response("https://shibbo.tul.cz/idp/profile/Authn/SAML2/POST/SSO", to_menza),
                           response("https://menza.tul.cz/", '<a href="/odhlasit/">Out</a>')])
    login(session, USER, PASS)
    assert [method for method, _, _ in session.calls] == ["GET", "POST", "POST", "POST", "POST"]
    assert session.calls[2][1] == MS + "/password"
    assert session.calls[2][2]["data"]["passwd"] == PASS


def test_no_password_post_forwarding_even_to_approved_host():
    session = FakeSession([response(MS + "/login", config()),
                           response(MS + "/check", '{"IfExistsResult":0}'),
                           response(MS + "/password", status=307, location="https://shibbo.tul.cz/next")])
    with pytest.raises(AuthError, match="forward"):
        login(session, USER, PASS)
    assert len(session.calls) == 3
    assert session.calls[-1][2]["data"]["passwd"] == PASS


def test_already_authenticated_shibboleth_step_does_not_ask_microsoft_for_password():
    form = '<form action="https://menza.tul.cz/Shibboleth.sso/SAML2/POST"><input name="SAMLResponse" value="synthetic" /></form>'
    session = FakeSession([response("https://shibbo.tul.cz/step", form),
                           response("https://menza.tul.cz/", '<a href="/odhlasit/">Out</a>')])
    login(session, USER, PASS)
    assert [method for method, _, _ in session.calls] == ["GET", "POST"]
    assert session.calls[1][1].startswith("https://menza.tul.cz/")


def test_microsoft_to_shibboleth_to_menza_saml_response_chain():
    to_idp = ('<form action="https://shibbo.tul.cz/idp/profile/Authn/SAML2/POST/SSO">'
              '<input name="RelayState" value="synthetic-relay" />'
              '<input name="SAMLResponse" value="synthetic-assertion" /></form>')
    to_menza = ('<form action="https://menza.tul.cz/Shibboleth.sso/SAML2/POST">'
                '<input name="RelayState" value="synthetic-relay" />'
                '<input name="SAMLResponse" value="synthetic-assertion" /></form>')
    session = FakeSession([response(MS + "/kmsi", to_idp),
                           response("https://shibbo.tul.cz/idp/profile/Authn/SAML2/POST/SSO", to_menza),
                           response("https://menza.tul.cz/", '<a href="/odhlasit/">Out</a>')])
    login(session, USER, PASS)
    assert [call[1] for call in session.calls[1:]] == [
        "https://shibbo.tul.cz/idp/profile/Authn/SAML2/POST/SSO",
        "https://menza.tul.cz/Shibboleth.sso/SAML2/POST",
    ]
    assert all(call[2]["allow_redirects"] is False for call in session.calls)


@pytest.mark.parametrize("source,action", [
    (MS + "/kmsi", "https://menza.tul.cz/Shibboleth.sso/SAML2/POST"),
    (MS + "/kmsi", "https://shibbo.tul.cz/idp/profile/other"),
    ("https://shibbo.tul.cz/idp/profile/Authn/SAML2/POST/SSO",
     "https://shibbo.tul.cz/idp/profile/Authn/SAML2/POST/SSO"),
    ("https://shibbo.tul.cz/idp/profile/Authn/SAML2/POST/SSO", "https://menza.tul.cz/other"),
])
def test_response_destination_must_match_origin_and_exact_acs(source, action):
    form = f'<form action="{action}"><input name="SAMLResponse" value="synthetic" /></form>'
    session = FakeSession([response(source, form)])
    with pytest.raises(AuthError, match="destination"):
        login(session, USER, PASS)
    assert len(session.calls) == 1


def test_unapproved_saml_destination_rejected():
    form = '<form action="https://untrusted.invalid/collect"><input name="SAMLResponse" value="synthetic" /></form>'
    session = FakeSession([response("https://shibbo.tul.cz/step", form)])
    with pytest.raises(AuthError):
        login(session, USER, PASS)
    assert len(session.calls) == 1


@pytest.mark.parametrize("page", ["<html>unknown login</html>", config(pgid="ConvergedTFA"),
                                    config(fShowHipChallenge=True)])
def test_unknown_interactive_and_captcha_stop_without_secrets(page):
    session = FakeSession([response(MS + "/login", page)])
    with pytest.raises(AuthError) as exc:
        login(session, USER, PASS)
    assert USER not in str(exc.value) and PASS not in str(exc.value)
    assert len(session.calls) == 1


def test_network_error_does_not_echo_session_url_or_credentials():
    session = FakeSession([requests.ConnectionError("secret query=private")])
    with pytest.raises(AuthError) as exc:
        login(session, USER, PASS)
    assert "private" not in str(exc.value)
