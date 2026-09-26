from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException, Request
from fastapi.responses import Response

from app.admin_auth import AdminAuth


def request(
    method: str = "GET", cookie: str = "", csrf: str = "", host: str = "localhost", origin: str = ""
) -> Request:
    headers = [(b"host", host.encode())]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    if csrf:
        headers.append((b"x-admin-csrf", csrf.encode()))
    if origin:
        headers.append((b"origin", origin.encode()))
    return Request({
        "type": "http",
        "method": method,
        "scheme": "http",
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 1234),
        "path": "/api/sessions",
        "headers": headers,
    })


class AdminAuthTests(unittest.TestCase):
    def test_password_cookie_survives_restart_but_not_code_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            auth = AdminAuth(data_dir, "test_admin")
            with patch.dict(os.environ, {"SPECTRA_ORGANIZATION_CODE": "test-code"}):
                with self.assertRaises(HTTPException) as error:
                    auth.require(request("POST"))
                self.assertEqual(error.exception.status_code, 401)

                with self.assertRaises(HTTPException) as error:
                    auth.login(request("POST"), Response(), "wrong-code")
                self.assertEqual(error.exception.status_code, 401)

                response = Response()
                login = auth.login(request("POST"), response, "test-code")
                self.assertIn("HttpOnly", response.headers["set-cookie"])
                self.assertIn("Max-Age=2592000", response.headers["set-cookie"])
                cookie = response.headers["set-cookie"].split(";", 1)[0]

                restarted = AdminAuth(data_dir, "test_admin")
                self.assertTrue(restarted.status(request(cookie=cookie))["authenticated"])
                with self.assertRaises(HTTPException) as error:
                    restarted.require(request("POST", cookie))
                self.assertEqual(error.exception.status_code, 403)
                restarted.require(request("POST", cookie, login["csrfToken"]))
                self.assertFalse(restarted.status(request(cookie="test_admin=forged"))["authenticated"])

            with patch.dict(os.environ, {"SPECTRA_ORGANIZATION_CODE": "new-code"}):
                self.assertFalse(restarted.status(request(cookie=cookie))["authenticated"])

    def test_unconfigured_code_disables_login(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            auth = AdminAuth(data_dir, "test_admin")
            with patch.dict(os.environ, {"SPECTRA_ORGANIZATION_CODE": ""}):
                with self.assertRaises(HTTPException) as error:
                    auth.login(request("POST"), Response(), "anything")
                self.assertEqual(error.exception.status_code, 503)

    def test_public_login_requires_https_origin(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            auth = AdminAuth(data_dir, "test_admin")
            with patch.dict(os.environ, {"SPECTRA_ORGANIZATION_CODE": "test-code"}):
                with self.assertRaises(HTTPException) as error:
                    auth.login(
                        request("POST", host="example.test", origin="http://example.test"),
                        Response(), "test-code",
                    )
                self.assertEqual(error.exception.status_code, 403)
                response = Response()
                auth.login(
                    request("POST", host="example.test", origin="https://example.test"),
                    response, "test-code",
                )
                self.assertIn("Secure", response.headers["set-cookie"])


if __name__ == "__main__":
    unittest.main()
