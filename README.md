# whatsapp-atendimento-agendamento

Base de infraestrutura em FastAPI para receber webhooks do WhatsApp, preparada
para Cloud Run e PostgreSQL/Supabase. Ainda não há bot, envio de mensagens,
agenda ou regras de negócio.

## Instalação local

Requer Python 3.12 e PostgreSQL acessível.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Preencha todas as variáveis do `.env`. A `DATABASE_URL` aceita os prefixos
`postgresql://`, `postgres://` ou `postgresql+asyncpg://` e é normalizada para o
driver assíncrono. Use `development`, `test` ou `production` em `ENVIRONMENT`.

## Execução e testes

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8080 --no-access-log
pytest -q
```

O access log padrão fica desabilitado para não registrar o token de verificação
presente na query string. A aplicação emite logs JSON apenas com metadados
seguros da requisição.

Para futuras migrações, após criar modelos:

```powershell
alembic revision --autogenerate -m "descricao"
alembic upgrade head
```

## Docker

```powershell
docker build -t whatsapp-atendimento-agendamento .
docker run --rm -p 8080:8080 --env-file .env -e PORT=8080 whatsapp-atendimento-agendamento
```

O container usa Python 3.12, executa como usuário não-root e respeita a variável
`PORT` fornecida pelo Cloud Run.
