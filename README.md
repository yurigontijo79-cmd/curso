# EIXO Formação / EIXO Trilhas — MVP + Fases 02/03/04/05

Implementação operacional com:
- geração + revisão + versionamento + jornada
- importação canônica curricular com preview/validate/apply
- validação estrutural, issues e versionamento da base

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

## Endpoints Fase 05 (importação canônica)
- `POST /api/v1/admin/curriculum/import-batches`
- `GET /api/v1/admin/curriculum/import-batches`
- `GET /api/v1/admin/curriculum/import-batches/{batch_id}`
- `POST /api/v1/admin/curriculum/import-batches/{batch_id}/validate`
- `POST /api/v1/admin/curriculum/import-batches/{batch_id}/apply`
- `GET /api/v1/admin/curriculum/import-batches/{batch_id}/issues`
- `GET /api/v1/admin/curriculum/versions`
- `GET /api/v1/admin/curriculum/versions/{version_id}`
- `GET /api/v1/admin/curriculum/preview-diff?batch_id=<id>&against_version_id=<id>`

## Testes
```bash
pytest -q
```
