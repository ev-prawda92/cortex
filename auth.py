"""
CORTEX Authentication
─────────────────────
Email/password + Google/GitHub OAuth authentication.

Provides:
    - Password hashing (PBKDF2-SHA256)
    - JWT token generation and validation
    - OAuth flow helpers for Google and GitHub
    - FastAPI dependencies for protected routes
"""
import os
import secrets
import hashlib
import hmac
import time
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import HTTPException, Request, Depends
from fastapi.responses import RedirectResponse

# ── Configuration ─────────────────────────────────────────────

SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = int(os.environ.get("JWT_EXPIRY_HOURS", "72"))
JWT_ISSUER = os.environ.get("JWT_ISSUER", "cortex")
PBKDF2_ITERATIONS = int(os.environ.get("PBKDF2_ITERATIONS", "600000"))

# OAuth credentials (set these in .env)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")

# Base URL for OAuth callbacks
BASE_URL = os.environ.get("BASE_URL", "http://localhost:3000")


# ── Password Hashing (PBKDF2 — no extra deps) ────────────────

def hash_password(password: str) -> str:
    """Hash a password using PBKDF2-SHA256."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"pbkdf2:sha256:{PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    try:
        parts = password_hash.split("$")
        if len(parts) != 3:
            return False
        scheme, salt, stored_hash = parts
        scheme_parts = scheme.split(":")
        if len(scheme_parts) != 3 or scheme_parts[:2] != ["pbkdf2", "sha256"]:
            return False
        iterations = int(scheme_parts[2])
        if iterations < 100_000 or iterations > 5_000_000:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
        return hmac.compare_digest(dk.hex(), stored_hash)
    except Exception:
        return False


# ── JWT Tokens (minimal, no PyJWT dependency) ─────────────────

import base64

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)

def create_token(user_id: str, email: str, name: str = "") -> str:
    """Create a JWT-like token (HS256)."""
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url_encode(json.dumps({
        "sub": user_id,
        "email": email,
        "name": name,
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_EXPIRY_HOURS * 3600,
        "iss": JWT_ISSUER,
        "jti": secrets.token_urlsafe(16),
    }).encode())
    signing_input = f"{header}.{payload}"
    signature = _b64url_encode(
        hmac.new(SECRET_KEY.encode(), signing_input.encode(), hashlib.sha256).digest()
    )
    return f"{header}.{payload}.{signature}"


def decode_token(token: str) -> Optional[dict]:
    """Decode and verify a token. Returns payload dict or None."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, payload, signature = parts
        header_data = json.loads(_b64url_decode(header))
        if header_data != {"alg": "HS256", "typ": "JWT"}:
            return None
        # Verify signature
        signing_input = f"{header}.{payload}"
        expected = _b64url_encode(
            hmac.new(SECRET_KEY.encode(), signing_input.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        # Decode payload
        data = json.loads(_b64url_decode(payload))
        # Check expiry
        if data.get("exp", 0) < time.time():
            return None
        if data.get("iss") != JWT_ISSUER:
            return None
        return data
    except Exception:
        return None


# ── OAuth Flows ───────────────────────────────────────────────

def get_google_auth_url(state: str) -> str:
    """Generate Google OAuth authorization URL."""
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": f"{BASE_URL}/api/auth/callback/google",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    qs = "&".join(f"{k}={httpx.URL('', params={k: v}).params[k]}" for k, v in params.items())
    return f"https://accounts.google.com/o/oauth2/v2/auth?{qs}"


def get_github_auth_url(state: str) -> str:
    """Generate GitHub OAuth authorization URL."""
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": f"{BASE_URL}/api/auth/callback/github",
        "scope": "user:email",
        "state": state,
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"https://github.com/login/oauth/authorize?{qs}"


async def exchange_google_code(code: str) -> dict:
    """Exchange Google auth code for user info."""
    async with httpx.AsyncClient() as client:
        # Exchange code for tokens
        token_resp = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": f"{BASE_URL}/api/auth/callback/google",
            "grant_type": "authorization_code",
        })
        token_resp.raise_for_status()
        tokens = token_resp.json()

        # Get user info
        user_resp = await client.get("https://www.googleapis.com/oauth2/v2/userinfo",
                                      headers={"Authorization": f"Bearer {tokens['access_token']}"})
        user_resp.raise_for_status()
        user_info = user_resp.json()

    return {
        "provider": "google",
        "oauth_id": user_info["id"],
        "email": user_info["email"],
        "name": user_info.get("name", ""),
        "avatar_url": user_info.get("picture", ""),
        "raw": user_info,
    }


async def exchange_github_code(code: str) -> dict:
    """Exchange GitHub auth code for user info."""
    async with httpx.AsyncClient() as client:
        # Exchange code for token
        token_resp = await client.post("https://github.com/login/oauth/access_token",
                                        headers={"Accept": "application/json"},
                                        data={
                                            "code": code,
                                            "client_id": GITHUB_CLIENT_ID,
                                            "client_secret": GITHUB_CLIENT_SECRET,
                                        })
        token_resp.raise_for_status()
        tokens = token_resp.json()
        access_token = tokens["access_token"]

        # Get user info
        user_resp = await client.get("https://api.github.com/user",
                                      headers={"Authorization": f"Bearer {access_token}"})
        user_resp.raise_for_status()
        user_info = user_resp.json()

        # Get email (may be private)
        email = user_info.get("email")
        if not email:
            emails_resp = await client.get("https://api.github.com/user/emails",
                                            headers={"Authorization": f"Bearer {access_token}"})
            emails_resp.raise_for_status()
            emails = emails_resp.json()
            primary = next((e for e in emails if e.get("primary")), emails[0] if emails else None)
            email = primary["email"] if primary else f"{user_info['login']}@github.noemail"

    return {
        "provider": "github",
        "oauth_id": str(user_info["id"]),
        "email": email,
        "name": user_info.get("name") or user_info.get("login", ""),
        "avatar_url": user_info.get("avatar_url", ""),
        "raw": user_info,
    }


# ── FastAPI Auth Dependency ───────────────────────────────────

def get_current_user(request: Request) -> dict:
    """Extract and validate the current user from the session cookie or Authorization header."""
    # Check cookie first
    token = request.cookies.get("session")

    # Then check Authorization header
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        raise HTTPException(401, "Not authenticated")

    payload = decode_token(token)
    if not payload:
        raise HTTPException(401, "Invalid or expired session")

    return payload


def get_optional_user(request: Request) -> Optional[dict]:
    """Like get_current_user but returns None instead of raising."""
    try:
        return get_current_user(request)
    except HTTPException:
        return None


# ── OAuth availability check ─────────────────────────────────

def oauth_providers_available() -> dict:
    """Return which OAuth providers are configured."""
    return {
        "google": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
        "github": bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET),
    }
