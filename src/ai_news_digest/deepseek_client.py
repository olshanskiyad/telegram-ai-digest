import json
import logging
from typing import Any

import httpx

from ai_news_digest.config import settings


logger = logging.getLogger(__name__)


class DeepSeekClient:
    def __init__(self) -> None:
        self.base_url = settings.DEEPSEEK_BASE_URL.rstrip("/")
        self.api_key = settings.DEEPSEEK_API_KEY.get_secret_value()
        self.chat_model = settings.DEEPSEEK_CHAT_MODEL
        self.reasoner_model = settings.DEEPSEEK_REASONER_MODEL

    async def json_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4000,
    ) -> dict[str, Any]:
        payload = {
            "model": model or self.chat_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        response_data = await self._post_chat_completion(payload)
        content = response_data["choices"][0]["message"]["content"]
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            logger.error("DeepSeek returned invalid JSON: %s", content[:500])
            raise RuntimeError(f"DeepSeek JSON parse error: {exc}") from exc

    async def _post_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()
