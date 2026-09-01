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

O catálogo de serviços guarda duração, preço e adicionais editáveis. O arquivo
`data/default_service_catalog.json` oferece templates versionados apenas para a
inicialização explícita de novas empresas; ele nunca atualiza configurações já
copiadas. Valores de referência incluem fonte e data, e serviços sem base segura
permanecem como `human_quote`.

Todo deslocamento parte da base operacional da empresa para o endereço do
cliente. A origem inicial `Zona Leste de São José dos Campos - SP` é ampla, não
possui coordenada inventada e fica marcada como imprecisa. O modo `route` usa um
`TravelTimePort` injetável e independente de fornecedor. O modo
`configured_estimate` aceita somente regras regionais marcadas como confiáveis;
um fallback em minutos precisa ser configurado e permitido explicitamente pela
empresa. Sem provider, regra confiável ou fallback autorizado, o agendamento vai
para atendimento humano.

O webhook processa somente remetentes individuais 1:1. Indicadores de grupo,
comunidade, canal/newsletter ou broadcast são descartados antes de qualquer
customer, conversation ou outbound, mantendo `200` para a Meta. Status legítimos
de mensagens continuam independentes desse filtro.

Serviços com peças/equipamentos, diagnóstico incerto, projeto comercial complexo
ou configuração insuficiente exigem atendimento humano sem questionário técnico.
O fluxo existente só pergunta os campos habilitados no serviço. O contrato para
um futuro serviço planejado pelo atendente, sem improvisar dados em
`conversation.context`, está em `docs/planned_services.md`.

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

Os valores comerciais da migration são defaults estruturais editáveis, não
cotações de mercado. A migration não importa o catálogo nem sobrescreve dados de
empresa. Preços sem configuração segura devem permanecer como `human_quote`.

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
