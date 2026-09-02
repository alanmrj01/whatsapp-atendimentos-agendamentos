# whatsapp-atendimento-agendamento

Backend FastAPI para atendimento e agendamento de serviços pelo WhatsApp,
preparado para Cloud Run e PostgreSQL/Supabase. Possui webhook assinado, outbox,
cliente isolado da Cloud API, motor determinístico, agenda PostgreSQL e envio
assíncrono opcional da outbox por Cloud Tasks.

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
Invoke-RestMethod http://localhost:8080/health
Invoke-RestMethod http://localhost:8080/ready
```

`GET /health/db` executa `SELECT 1`: responde `200` quando o PostgreSQL está
acessível e `503` sem expor detalhes da conexão quando está indisponível.
`GET /health` confirma somente que o processo está vivo. `GET /ready` executa a
mesma verificação segura do PostgreSQL e responde `503` enquanto a dependência
necessária não estiver disponível.

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
PostgreSQL descartável local cujo nome contenha `test`; qualquer host que não seja
loopback e hosts Supabase são bloqueados. O compose de teste usa PostgreSQL 16,
porta local `55432`, healthcheck e armazenamento efêmero sem volume compartilhado.

```powershell
$env:POSTGRES_TEST_PASSWORD="<senha-local-descartavel>"
docker compose -f docker-compose.test.yml up -d --wait
$env:TEST_DATABASE_URL="postgresql+asyncpg://whatsapp_test:$($env:POSTGRES_TEST_PASSWORD)@127.0.0.1:55432/whatsapp_test"
pytest tests/integration/test_booking_postgresql.py -vv -p no:cacheprovider
docker compose -f docker-compose.test.yml down --volumes
Remove-Item Env:TEST_DATABASE_URL,Env:POSTGRES_TEST_PASSWORD
```

Sem `TEST_DATABASE_URL`, os dez cenários A–J e as quatro validações físicas
suplementares aparecem como `skipped` de forma explícita. Nunca aponte essa
variável para produção: a suíte aplica as migrations, limpa dados de teste e
executa downgrade até `base` ao terminar.

## Docker

```powershell
docker build -t whatsapp-atendimento-agendamento .
docker run --rm -p 8080:8080 --env-file .env -e PORT=8080 whatsapp-atendimento-agendamento
```

O container usa Python 3.12, executa como usuário não-root e respeita a variável
`PORT` fornecida pelo Cloud Run. A imagem define `ENVIRONMENT=production`, inicia
um único processo Uvicorn e encerra graciosamente ao receber `SIGTERM`. Migrations
Alembic não são executadas no startup do container.

Em produção, o startup exige somente `DATABASE_URL`, preferencialmente com o
Transaction Pooler Supabase `:6543` via Secret Manager, e
`ENVIRONMENT=production`. O Cloud Run fornece `PORT`. As variáveis `META_*` são
opcionais até a integração WhatsApp ser ativada; sem os secrets necessários, o
webhook falha fechado com resposta segura e nenhuma persistência. Quando ativar
a integração, configure `META_ACCESS_TOKEN`, `META_APP_SECRET` e
`META_VERIFY_TOKEN` como secrets, além de `META_PHONE_NUMBER_ID`, `META_WABA_ID` e
`META_GRAPH_VERSION`. Use `ALEMBIC_DATABASE_URL` apenas no processo separado de
migrations, com conexão Direct ou Session `:5432`.

## Cloud Tasks inbound

`CLOUD_TASKS_ENABLED=false` mantém o processamento síncrono atual. Quando a flag
é habilitada, o webhook confirma assinatura e remetente 1:1, persiste o evento de
forma idempotente, encerra a transação e publica uma task determinística contendo
somente `event_key`. O worker autenticado em
`POST /internal/tasks/whatsapp-event` busca os dados normalizados no PostgreSQL e
executa o motor conversacional sem enviar a outbox ao WhatsApp.

Configure `GCP_PROJECT_ID`, `GCP_REGION`, `CLOUD_TASKS_EVENTS_QUEUE`,
`CLOUD_TASKS_TARGET_URL`, `CLOUD_TASKS_OIDC_AUDIENCE` e
`CLOUD_TASKS_INVOKER_EMAIL`. A identidade esperada em produção é
`whatsapp-task-invoker@whatsapp-automacao-prod.iam.gserviceaccount.com`; ela deve
ter permissão de Cloud Run Invoker. Falhas de enqueue retornam erro transitório
para permitir retry, enquanto nomes repetidos (`AlreadyExists`) são tratados como
sucesso. Payload bruto, telefone e conteúdo de mensagem nunca entram na task.

## Cloud Tasks outbound

`OUTBOUND_TASKS_ENABLED=false` preserva a outbox sem envio automático. Quando a
flag é habilitada, cada mensagem `pending` criada pelo motor é localizada depois
do commit e publicada na fila `whatsapp-outbound` com nome determinístico e body
contendo somente `message_id`. O endpoint autenticado
`POST /internal/tasks/whatsapp-outbound` bloqueia a linha no PostgreSQL e usa o
`WhatsAppClient` para texto, botões ou lista.

Configure `CLOUD_TASKS_OUTBOUND_QUEUE=whatsapp-outbound` e
`CLOUD_TASKS_OUTBOUND_TARGET_URL`, reutilizando projeto, região, audience e conta
OIDC do inbound. Timeout, limite de taxa e erros 5xx mantêm a mensagem `pending`
e retornam erro para retry do Cloud Tasks. Erros 4xx permanentes resultam em
`failed`; `sent`, `delivered`, `read` e `failed` nunca são reenviados. A task e os
logs não incluem destinatário, conteúdo, token ou payload da mensagem.

O lock transacional impede dois workers locais de enviarem a mesma linha ao
mesmo tempo. Não existe garantia exactly-once no sistema externo: se a Meta
aceitar a mensagem e o processo morrer antes de persistir o identificador, um
retry poderá produzir novo envio. Não há migration nem alteração de schema nesta
etapa. Com a feature desligada, nenhuma configuração `META_*` adicional é exigida
no startup; as credenciais só são validadas quando um envio habilitado é executado.
