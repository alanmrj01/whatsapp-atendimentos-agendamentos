# whatsapp-atendimento-agendamento

Backend FastAPI para atendimento e agendamento de serviços pelo WhatsApp,
preparado para Cloud Run e PostgreSQL/Supabase. Possui webhook assinado, outbox,
cliente isolado da Cloud API, motor determinístico e agenda PostgreSQL. O envio
automático da outbox ainda não faz parte desta etapa.

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

O catálogo de serviços guarda duração, preço e adicionais editáveis. A origem
operacional padrão é `Zona Leste de São José dos Campos - SP`, também editável
por empresa. O fallback de deslocamento usa minutos e regras regionais
configurados no banco; não exige API paga e não representa rota/GPS real.

Para aplicar as migrations em uma conexão Direct ou Session do Supabase:

```powershell
$env:ALEMBIC_DATABASE_URL="postgresql://...:5432/postgres"
alembic upgrade head
```

Para migrations, defina `ALEMBIC_DATABASE_URL` com uma conexão Direct ou Session
`:5432`. Se ela estiver vazia, o Alembic usa `DATABASE_URL`. O Alembic continuará
sendo a única fonte de alterações de schema; a aplicação nunca usa
`create_all()`. A migration `20260901_0003` adiciona apenas configurações de
serviço/deslocamento e snapshots históricos do agendamento. Ela não é aplicada
automaticamente a nenhum projeto Supabase.

Os valores comerciais da migration são defaults operacionais editáveis, não
cotações de mercado. Preços sem configuração segura devem permanecer como
`estimated` ou `human_quote`.

## Testes PostgreSQL físicos

Os testes unitários não acessam rede nem Supabase. A suíte física A–J exige um
PostgreSQL descartável cujo nome contenha `test`; hosts Supabase são bloqueados.

```powershell
$env:TEST_DATABASE_URL="postgresql://usuario:senha@localhost:5432/booking_test"
pytest tests/integration/test_booking_postgresql.py -q
```

Sem `TEST_DATABASE_URL`, esses dez testes aparecem como `skipped` de forma
explícita. Nunca aponte essa variável para produção: a suíte aplica as migrations,
limpa dados de teste e executa downgrade até `base` ao terminar.

## Docker

```powershell
docker build -t whatsapp-atendimento-agendamento .
docker run --rm -p 8080:8080 --env-file .env -e PORT=8080 whatsapp-atendimento-agendamento
```

O container usa Python 3.12, executa como usuário não-root e respeita a variável
`PORT` fornecida pelo Cloud Run.
