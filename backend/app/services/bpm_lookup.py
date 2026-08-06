"""BPM/tempo lookup: GetSongBPM primary, LLM estimate fallback (KTD9, AE1).

GetSongBPM: free, artist+title lookup, 3,000 req/hour rate limit, and a
mandatory attribution credit in the UI (surfaced by U8, not here).
"""

from dataclasses import dataclass
from typing import Literal, Optional

import litellm
import requests
from litellm import completion
from pydantic import BaseModel

from app.core.config import get_settings
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.integrations.http_client import CircuitBreaker, CircuitOpenError, call_with_retry

litellm.suppress_debug_info = True

BPM_API_URL = "https://api.getsongbpm.com/search/"
ESTIMATE_MODEL = "openrouter/google/gemini-2.5-flash"

BpmSource = Literal["measured", "estimated"]


class _BpmEstimate(BaseModel):
    bpm: float


@dataclass
class BpmLookupResult:
    bpm: Optional[float]
    source: Optional[BpmSource]  # None only when both the lookup and the LLM estimate failed


class BpmLookupService:
    def __init__(self, api_key: str | None = None, openrouter_api_key: str | None = None):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.getsongbpm_api_key
        self.openrouter_api_key = (
            openrouter_api_key if openrouter_api_key is not None else settings.openrouter_api_key
        )
        self._circuit_breaker = CircuitBreaker()

    def lookup_bpm(self, artist: str, title: str) -> BpmLookupResult:
        measured = self._lookup_measured(artist, title)
        if measured is not None:
            dependency_health_store.set_status("getsongbpm", DependencyStatus.OK)
            return BpmLookupResult(bpm=measured, source="measured")

        estimated = self._estimate_with_llm(artist, title)
        return BpmLookupResult(bpm=estimated, source="estimated" if estimated is not None else None)

    def _lookup_measured(self, artist: str, title: str) -> Optional[float]:
        if not self.api_key:
            return None

        try:
            resp = call_with_retry(
                lambda: requests.get(
                    BPM_API_URL,
                    params={
                        "api_key": self.api_key,
                        "type": "both",
                        "lookup": f"song:{title} artist:{artist}",
                    },
                    timeout=10,
                ),
                retries=2,
                delay_s=1.0,
                retry_on=(requests.RequestException,),
                circuit_breaker=self._circuit_breaker,
            )
        except (requests.RequestException, CircuitOpenError) as exc:
            # Distinguishable from a genuine miss (KTD18): surfaced as degraded,
            # not silently treated as "not found." CircuitOpenError must be
            # caught here too — it's raised by call_with_retry's
            # circuit_breaker.before_call(), not by the request itself, so
            # it isn't a requests.RequestException.
            dependency_health_store.set_status(
                "getsongbpm", DependencyStatus.DEGRADED, f"GetSongBPM lookup failed: {exc}"
            )
            return None

        if resp.status_code == 429:
            dependency_health_store.set_status(
                "getsongbpm", DependencyStatus.DEGRADED, "GetSongBPM rate limit hit (429)"
            )
            return None

        try:
            resp.raise_for_status()
            results = resp.json().get("search", [])
        except (requests.RequestException, ValueError) as exc:
            dependency_health_store.set_status(
                "getsongbpm", DependencyStatus.DEGRADED, f"GetSongBPM response error: {exc}"
            )
            return None

        if not results or "tempo" not in results[0]:
            return None  # genuine miss — this artist/title just isn't in GetSongBPM

        try:
            return float(results[0]["tempo"])
        except (TypeError, ValueError) as exc:
            # A present-but-malformed tempo field is a response error, not a
            # genuine miss — must not raise past this method (KTD18).
            dependency_health_store.set_status(
                "getsongbpm", DependencyStatus.DEGRADED, f"GetSongBPM malformed tempo field: {exc}"
            )
            return None

    def _estimate_with_llm(self, artist: str, title: str) -> Optional[float]:
        if not self.openrouter_api_key:
            return None

        schema = _BpmEstimate.model_json_schema()

        def _call():
            return completion(
                model=ESTIMATE_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "Estimate the tempo (BPM) of the given song using your own knowledge. Respond with your best single-number estimate.",
                    },
                    {"role": "user", "content": f"{artist} - {title}"},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "bpm_estimate", "schema": schema, "strict": True},
                },
                temperature=0.0,
                max_tokens=200,
            )

        try:
            resp = call_with_retry(
                _call,
                retries=2,
                delay_s=1.0,
                retry_on=(litellm.exceptions.APIError,),
                circuit_breaker=self._circuit_breaker,
            )
            raw = resp.choices[0].message.content or ""
            parsed = _BpmEstimate.model_validate_json(raw.strip())
        except Exception as exc:
            # KTD18: caught broadly and deliberately — this is the per-song
            # fault-isolation boundary for the LLM estimate fallback. Uses its
            # own health-store key (distinct from description-match/clustering,
            # KTD17) so one LLM use case's failure can't mask another's.
            dependency_health_store.set_status(
                "llm_bpm_estimate", DependencyStatus.DEGRADED, f"BPM estimate failed: {exc}"
            )
            return None

        dependency_health_store.set_status("llm_bpm_estimate", DependencyStatus.OK)
        return parsed.bpm
