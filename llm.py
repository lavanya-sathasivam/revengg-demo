"""One small LLM helper. Owner: Person B (branch b-graph).

WHY THIS FILE EXISTS
    The system has exactly ONE job for the LLM: read one clause of English and
    tell us what legal structure it contains (what it creates, what it depends
    on). Everything else - linking, traversing, dates, doubts - is deterministic
    Python, so the demo still works when the API is down or the key expires.

DESIGN RULES
    1. ask_json() NEVER raises. On any failure it returns {} and the caller
       falls back to the rule-based path.
    2. No provider is hard-wired. Set LLM_PROVIDER=gemini|openai|mock in .env.
    3. The API key is read from the environment (.env). Never commit a key.

.env example:
    LLM_PROVIDER=gemini
    GEMINI_API_KEY=your-key-here
"""

import json
import os
import re

# Load .env if python-dotenv is installed (it is, but do not hard-depend on it).
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


PROVIDER = os.environ.get("LLM_PROVIDER", "mock").strip().lower()

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def provider_available():
    """True when a real provider is configured and has a key."""
    if PROVIDER == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    if PROVIDER == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY"))
    return False


def _call_openai(prompt):
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You output strict JSON and nothing else."},
            {"role": "user", "content": prompt},
        ],
    )
    return resp.choices[0].message.content


def _call_gemini(prompt):
    import google.generativeai as genai

    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel(
        os.environ.get("LLM_MODEL", "gemini-2.0-flash"),
        generation_config={"response_mime_type": "application/json", "temperature": 0},
    )
    return model.generate_content(prompt).text


def ask_json(prompt):
    """Ask the model for a JSON object. Returns {} on any problem."""
    if PROVIDER == "mock" or not provider_available():
        return {}
    try:
        raw = _call_gemini(prompt) if PROVIDER == "gemini" else _call_openai(prompt)
        if not raw:
            return {}
        match = _JSON_BLOCK.search(raw)
        if not match:
            return {}
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        # A demo must never die because a network call failed.
        return {}
