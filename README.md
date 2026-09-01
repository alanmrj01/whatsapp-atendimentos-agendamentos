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

Preencha todas as variáveis do `.env`. As URLs aceitam os prefixos
`postgresql://`, `postgres://` ou `postgresql+asyncpg://` e são normalizadas para
o driver assíncrono. Use `development`, `test` ou `production` em `ENVIRONMENT`.

No Supabase, obtenha as URIs em **Dashboard > Connect**. Use em `DATABASE_URL` o
Session Pooler `:5432` para execução local ou o Transaction Pooler `:6543` no
Cloud Run. O modo transaction é detectado automaticamente e usa `NullPool`, sem
cache de statements/prepared statements. Consulte a
[documentação oficial de conexão](https://supabase.com/docs/guides/database/connecting-to-postgres),
copie as URIs para o `.env` local e nunca versione esse arquivo.

## Execução e testes

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8080 --no-access-log
pytest -q
Invoke-RestMethod http://localhost:8080/health/db
```

`GET /health/db` executa `SELECT 1`: responde `200` quando o PostgreSQL está
acessível e `503` sem expor detalhes da conexão quando está indisponível.

O access log padrão fica desabilitado para não registrar o token de verificação
presente na query string. A aplicação emite logs JSON apenas com metadados
seguros da requisição.

Para futuras migrações, após criar modelos:

```powershell
alembic revision --autogenerate -m "descricao"
alembic upgrade head
```

Para migrations, defina `ALEMBIC_DATABASE_URL` com uma conexão Direct ou Session
`:5432`. Se ela estiver vazia, o Alembic usa `DATABASE_URL`. O Alembic continuará
sendo a única fonte de alterações de schema; as tabelas de negócio serão criadas
somente na próxima etapa.

## Docker

```powershell
docker build -t whatsapp-atendimento-agendamento .
docker run --rm -p 8080:8080 --env-file .env -e PORT=8080 whatsapp-atendimento-agendamento
```

O container usa Python 3.12, executa como usuário não-root e respeita a variável
`PORT` fornecida pelo Cloud Run.
