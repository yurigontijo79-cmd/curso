from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


class GenerationEngineError(RuntimeError):
    pass


@dataclass
class GeneratedBlock:
    block_order: int
    block_kind: str
    title: str
    prompt_payload_json: dict[str, Any]
    raw_response_text: str
    normalized_content_markdown: str
    status: str = "generated"


class GenerationEngine:
    provider_name: str | None = None
    model_name: str | None = None
    execution_mode: str = "mock"

    def generate_piece(self, context: dict[str, Any]) -> list[GeneratedBlock]:
        raise NotImplementedError


class MockGenerationEngine(GenerationEngine):
    provider_name = "mock"
    model_name = "mock-v1"
    execution_mode = "mock"

    def generate_piece(self, context: dict[str, Any]) -> list[GeneratedBlock]:
        blocks = context["blocks"]
        out: list[GeneratedBlock] = []
        for i, block in enumerate(blocks, start=1):
            md = (
                f"## {block['title']}\n\n"
                f"Objetivo do bloco: {block['objective']}\n\n"
                f"Conteúdo curricular: {', '.join(context['program_content'])}."
            )
            out.append(
                GeneratedBlock(
                    block_order=i,
                    block_kind=block["kind"],
                    title=block["title"],
                    prompt_payload_json={"block": block, "mode": "mock"},
                    raw_response_text=md,
                    normalized_content_markdown=md,
                )
            )
        return out


class OpenAIGenerationEngine(GenerationEngine):
    execution_mode = "real_llm"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.provider_name = settings.llm_provider
        self.model_name = settings.llm_model
        if not settings.llm_api_key:
            raise GenerationEngineError("LLM_API_KEY ausente para geração real")

    def _call(self, prompt: str) -> str:
        url = f"{self.settings.llm_base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.settings.llm_model,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_output_tokens,
            "messages": [
                {"role": "system", "content": "Você é um motor de materialização curricular da EIXO."},
                {"role": "user", "content": prompt},
            ],
        }
        last_error: Exception | None = None
        for _ in range(self.settings.llm_max_retries + 1):
            try:
                with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
                    resp = client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"].strip()
            except Exception as exc:
                last_error = exc
        raise GenerationEngineError(f"Falha na chamada real: {last_error}")

    def generate_piece(self, context: dict[str, Any]) -> list[GeneratedBlock]:
        blocks = context["blocks"]
        generated: list[GeneratedBlock] = []
        accumulated = ""
        for i, block in enumerate(blocks, start=1):
            prompt_payload = {
                "role": "motor curricular",
                "target": context["target"],
                "syllabus": context["syllabus"],
                "block": block,
                "style_rules": context["style_rules"],
                "forbidden": context["forbidden"],
                "accumulated_context": accumulated,
                "output": "markdown objetivo e direto",
            }
            prompt = json.dumps(prompt_payload, ensure_ascii=False)
            raw = self._call(prompt)
            normalized = raw.strip()
            generated.append(
                GeneratedBlock(
                    block_order=i,
                    block_kind=block["kind"],
                    title=block["title"],
                    prompt_payload_json=prompt_payload,
                    raw_response_text=raw,
                    normalized_content_markdown=normalized,
                )
            )
            accumulated += "\n\n" + normalized
        return generated


def get_engine(settings: Settings) -> GenerationEngine:
    if settings.generation_mode == "real_llm":
        return OpenAIGenerationEngine(settings)
    return MockGenerationEngine()
