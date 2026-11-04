"""Google Cloud Vertex AI: Gemini for the agents and photo checks, Gemini image generation for the shop.

Credentials come from Application Default Credentials: `gcloud auth application-default login` on a
laptop, the service account on Cloud Run (it needs roles/aiplatform.user). VERTEX_PROJECT turns
Vertex on; VERTEX_LOCATION defaults to "global", where the newest Gemini models are served.
"""

from __future__ import annotations

import base64
import os
import threading
import time

import httpx

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# The dispute agent, via the OpenAI-compatible endpoint. Gemini 3 Flash (preview) writes slightly richer
# notes but hit Vertex's preview rate limits with four investigations at once; 2.5 Flash is GA.
AGENT_MODEL = "google/gemini-2.5-flash"
INSIGHTS_MODEL = "google/gemini-2.5-flash"        # AG Studio's assistant (see web.py for why not Gemini 3)
VISION_MODEL = "gemini-3-flash-preview"           # photo comparisons
IMAGE_MODEL = "gemini-2.5-flash-image"            # product and buyer photos


def project() -> str | None:
    return os.environ.get("VERTEX_PROJECT")


def location() -> str:
    return os.environ.get("VERTEX_LOCATION", "global")


def host() -> str:
    loc = location()
    return "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"


def openai_base(loc: str | None = None) -> str:
    """Vertex's OpenAI-compatible Chat Completions endpoint (base URL, with a trailing slash)."""
    loc = loc or location()
    h = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
    return f"https://{h}/v1/projects/{project()}/locations/{loc}/endpoints/openapi/"


# Where to retry when one location's shared capacity is exhausted (HTTP 429). GA models only:
# preview models are served from "global" alone.
FALLBACK_LOCATIONS = ["global", "us-central1", "europe-west4", "us-east5"]


class _Token:
    """An access token from Application Default Credentials, refreshed a few minutes before it expires."""

    def __init__(self):
        self._lock = threading.Lock()
        self._creds = None

    def __call__(self) -> str:
        with self._lock:
            if self._creds is None:
                import google.auth
                self._creds, _ = google.auth.default(scopes=[SCOPE])
            expiry = getattr(self._creds, "expiry", None)
            if not self._creds.valid or (expiry and expiry.timestamp() - time.time() < 300):
                import google.auth.transport.requests
                self._creds.refresh(google.auth.transport.requests.Request())
            return self._creds.token


token = _Token()


def generate(model: str, parts: list[dict], *, image: bool = False, timeout: float = 120,
             system: str | None = None, json_schema: dict | None = None) -> dict:
    """One generateContent call. `parts` are Gemini parts: {"text": ...} or {"inlineData": {...}}."""
    body: dict = {"contents": [{"role": "user", "parts": parts}]}
    config: dict = {}
    if image:
        config["responseModalities"] = ["IMAGE"]
    if json_schema:
        config.update(responseMimeType="application/json", responseSchema=json_schema)
    if config:
        body["generationConfig"] = config
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    url = (f"https://{host()}/v1/projects/{project()}/locations/{location()}"
           f"/publishers/google/models/{model}:generateContent")
    for attempt in range(5):
        r = httpx.post(url, json=body, timeout=timeout, headers={"Authorization": f"Bearer {token()}"})
        if r.status_code not in (429, 500, 503):
            break
        time.sleep(5 * 2 ** attempt)   # 5 s up to 80 s: image and preview models have tight per-minute quotas
    r.raise_for_status()
    return r.json()


def text_of(response: dict) -> str:
    parts = (response.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def image_of(response: dict) -> bytes:
    for p in (response.get("candidates") or [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in p:
            return base64.b64decode(p["inlineData"]["data"])
    raise ValueError("the model returned no image: " + text_of(response)[:200])


def inline(data: bytes, mime: str = "image/jpeg") -> dict:
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}}
