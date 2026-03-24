# EIXO Formação / EIXO Trilhas — MVP + Fases 02/03/04

Implementação operacional com:
- árvore curricular + ementa vigente
- geração por blocos com persistência e reuso
- revisão/aprovação/rejeição/versionamento editorial
- trilha adaptativa local e explicável (sem IA opaca)
- retomada inteligente e recomendações discretas

## Configuração
```bash
export GENERATION_MODE=mock
export LLM_PROVIDER=openai
export LLM_MODEL=gpt-4.1-mini
export LLM_API_KEY=***
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_TIMEOUT_SECONDS=30
export LLM_MAX_RETRIES=2
export LLM_TEMPERATURE=0.2
export LLM_MAX_OUTPUT_TOKENS=900
export ALLOW_MOCK_FALLBACK=false

export JOURNEY_PAUSED_AFTER_HOURS=24
export JOURNEY_ABANDONED_AFTER_DAYS=7
export REVIEW_SUGGEST_AFTER_DAYS=14
export RECENT_ACTIVITY_WINDOW_HOURS=12
```

## Executar
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Endpoints de jornada (Fase 04)
- `GET /api/v1/journey/me/next-step`
- `GET /api/v1/journey/me/recommendations`
- `GET /api/v1/journey/me/resume`
- `POST /api/v1/journey/me/recommendations/{id}/dismiss`

## Testes
```bash
pytest -q
```
