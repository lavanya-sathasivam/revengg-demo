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
    4. The provider is resolved at CALL time, not at import time, so that
       changing .env does not need a restart and so that the LLM path can be
       exercised in a test without a network.

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


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def provider_name():
    """The configured provider: 'gemini', 'openai' or 'mock'.

    Resolved on every call so a .env change takes effect without a restart.
    """
    return os.environ.get("LLM_PROVIDER", "mock").strip().lower()


def provider_available():
    """True when a real provider is configured and has a key."""
    provider = provider_name()
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    if provider == "gemini":
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
    """Call Gemini.

    Uses the current `google-genai` SDK, and falls back to the deprecated
    `google-generativeai` SDK if that is the one installed, so this keeps
    working on a machine that has the old package. Neither SDK is in
    requirements.txt: the core demo needs no key, so the SDK is optional.
    """
    model = os.environ.get("LLM_MODEL", "gemini-2.5-flash")
    try:
        from google import genai
    except ImportError:
        import google.generativeai as legacy

        legacy.configure(api_key=os.environ["GEMINI_API_KEY"])
        model_obj = legacy.GenerativeModel(
            model,
            generation_config={"response_mime_type": "application/json",
                               "temperature": 0},
        )
        return model_obj.generate_content(prompt).text

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={"response_mime_type": "application/json", "temperature": 0},
    )
    return getattr(response, "text", "") or ""


def ask_json(prompt):
    """Ask the model for a JSON object. Returns {} on any problem."""
    provider = provider_name()
    if provider == "mock" or not provider_available():
        return {}
    try:
        raw = (_call_gemini(prompt) if provider == "gemini"
               else _call_openai(prompt))
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
