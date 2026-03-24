# EIXO Formação / EIXO Trilhas — MVP + Fases 02..06

Implementação operacional com:
- geração + revisão + versionamento + jornada
- importação canônica curricular (batch/validate/apply/version)
- fila operacional de jobs, dedupe, claim/lock, retry e ações em lote

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

## Endpoints Fase 06 (fila e backlog)
- `POST /api/v1/admin/jobs`
- `GET /api/v1/admin/jobs`
- `GET /api/v1/admin/jobs/{job_id}`
- `POST /api/v1/admin/jobs/{job_id}/claim`
- `POST /api/v1/admin/jobs/{job_id}/cancel`
- `POST /api/v1/admin/jobs/{job_id}/retry`
- `GET /api/v1/admin/jobs/{job_id}/logs`
- `POST /api/v1/admin/batch-actions`
- `GET /api/v1/admin/batch-actions`
- `GET /api/v1/admin/backlog/summary`
- `GET /api/v1/admin/backlog/review`

## Testes
```bash
pytest -q
```
