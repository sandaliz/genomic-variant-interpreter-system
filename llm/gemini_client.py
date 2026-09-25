"""
llm/gemini_client.py

Central Gemini (Google Generative AI) client wrapper.

Handles:
  - Loading the API key and model from .env
  - Sending prompts to the Gemini API
  - Graceful fallback when the API key is missing or the call fails
  - Automatic retry with a fallback model if the primary model is overloaded
"""

import os
import sys
import time
import hashlib
from functools import lru_cache

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


class GeminiClient:
    """
    Thin wrapper around the Google GenAI SDK.

    The client is created lazily so the rest of the pipeline can be
    unit-tested even when no API key is configured.
    """

    # Fallback model chain: try these in order if the primary model is unavailable
    FALLBACK_MODELS = [
        "gemini-flash-lite-latest",  # Faster model by default for performance
        "gemini-2.5-flash-lite",
        "gemini-flash-latest",
    ]

    def __init__(self, fast_mode: bool = True, enable_llm_cache: bool = True):
        self._client = None
        self.api_key = os.getenv("GEMINI_API_KEY", "")
        # Use faster model by default for better performance
        default_model = "gemini-flash-lite-latest" if fast_mode else "gemini-flash-latest"
        self.model = os.getenv("GEMINI_MODEL", default_model)
        self.enable_llm_cache = enable_llm_cache
        self._llm_cache = {}  # Simple in-memory cache for LLM responses

    @property
    def available(self) -> bool:
        return bool(self.api_key) and self.api_key != "your-gemini-api-key-here"

    def _generate_cache_key(self, prompt: str, system_instruction: str = None, max_tokens: int = 1024) -> str:
        """Generate a cache key for LLM requests."""
        cache_string = f"{prompt}:{system_instruction}:{max_tokens}:{self.model}"
        return hashlib.md5(cache_string.encode()).hexdigest()

    def _get_client(self):
        """Lazy-create the Gemini client."""
        if self._client is None:
            from google import genai
            http_options = self._bounded_http_options()
            kwargs = {"api_key": self.api_key}
            if http_options is not None:
                kwargs["http_options"] = http_options
            self._client = genai.Client(**kwargs)
        return self._client

    # Cap the SDK's built-in exponential retry. Defaults allow up to 5
    # attempts with delays doubling toward 60s, so a Gemini 503 (high demand)
    # stalls a request ~11-20s invisibly before failing -- unacceptable
    # behind a web proxy. Measured locally: 10.96s and 6.10s stalls on 503.
    _RETRY_CAP = {"attempts": 2, "initial_delay": 1.0, "max_delay": 4.0}

    @classmethod
    def _bounded_http_options(cls):
        """HTTP options with a bounded retry, best-effort across SDK versions.

        If a future google-genai release changes the options schema, skip the
        cap instead of breaking client construction (graceful degradation via
        the APIError handling in generate() still applies).
        """
        from google.genai import types as genai_types
        for key in ("retry_options", "retry"):
            try:
                return genai_types.HttpOptions(**{key: dict(cls._RETRY_CAP)})
            except Exception:  # noqa: BLE001 - schema probe, try next key
                continue
        return None

    def generate(self, prompt: str, system_instruction: str = None, max_tokens: int = 1024) -> str:
        """
        Send a prompt to Gemini and return the text response.

        Tries the configured model first, then falls back through
        FALLBACK_MODELS if the model is unavailable or overloaded.

        Args:
            prompt: The user/content prompt to send.
            system_instruction: Optional system-level instruction.
            max_tokens: Maximum number of output tokens.

        Returns:
            The generated text from Gemini. Raises RuntimeError if the client is unavailable.

        Raises:
            RuntimeError: If no valid API key is configured or all models fail.
        """
        if not self.available:
            raise RuntimeError(
                "Gemini API key not set in .env. "
                "Create a .env file with GEMINI_API_KEY=<your key>."
            )

        # google.genai.errors.APIError (e.g. 503 UNAVAILABLE, 429) does NOT
        # inherit from ConnectionError/TimeoutError/RuntimeError, so without
        # listing it here it escaped the fallback loop entirely and crashed
        # the request after the SDK's internal retries.
        from google.genai import errors as genai_errors

        # Check cache first if enabled
        if self.enable_llm_cache:
            cache_key = self._generate_cache_key(prompt, system_instruction, max_tokens)
            if cache_key in self._llm_cache:
                print(f"[llm] Cache hit for request")
                return self._llm_cache[cache_key]

        client = self._get_client()
        models_to_try = list(dict.fromkeys([self.model] + self.FALLBACK_MODELS))

        last_error = None
        for model in models_to_try:
            try:
                result = self._generate_with_model(client, model, prompt, system_instruction, max_tokens)
                
                # Cache the result if enabled
                if self.enable_llm_cache:
                    cache_key = self._generate_cache_key(prompt, system_instruction, max_tokens)
                    self._llm_cache[cache_key] = result
                    print(f"[llm] Cached result (cache size: {len(self._llm_cache)})")
                
                return result
            except (ConnectionError, TimeoutError, RuntimeError,
                    genai_errors.APIError) as e:
                last_error = e
                # Includes 503 overloaded / 429 rate limit: move to the next
                # fallback model instead of crashing the request. If every
                # model fails we raise RuntimeError below, which the
                # classification/report agents already catch to fall back to
                # rule-based scoring and the template report.
                continue

        raise RuntimeError(
            f"All Gemini models failed. Last error: {last_error}"
        )

    def _generate_with_model(self, client, model: str, prompt: str,
                             system_instruction: str, max_tokens: int) -> str:
        if system_instruction:
            from google.genai import types
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=max_tokens,
                temperature=0.2,
            )
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
        else:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
            )

        return response.text.strip()


if __name__ == "__main__":
    client = GeminiClient()
    print(f"Gemini client available: {client.available}")
    if client.available:
        print(f"Primary model: {client.model}")
        text = client.generate("Say hello in one sentence.")
        print(f"Response: {text}")
    else:
        print("No API key found. Create a .env file with GEMINI_API_KEY=<your key>")