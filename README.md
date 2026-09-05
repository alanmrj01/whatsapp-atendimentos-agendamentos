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
`GET /health` confirma somente que o processo está vivo, sem consultar dependências.
`GET /ready` verifica inicialização, PostgreSQL e schema esperado `20260902_0005`:
retorna `200` com `{"status":"ready","database":"connected"}` ou `503` com
`status=not_ready`. Banco acessível com schema incompatível continua indicando
`database=connected`. Nenhum desses endpoints executa migrations.

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

## Diagnóstico operacional privado

`GET /internal/diagnostics` exige Bearer OIDC Google válido, com audience e email
verificado iguais aos configurados. Usa `CLOUD_TASKS_OIDC_AUDIENCE` e
`CLOUD_TASKS_INVOKER_EMAIL` mesmo com as filas desligadas. Uma identidade dedicada
pode ser definida pelo par `DIAGNOSTICS_OIDC_AUDIENCE` /
`DIAGNOSTICS_INVOKER_EMAIL`; um par incompleto falha fechado (`503`). Não há
acesso anônimo, inclusive local. Autenticação inválida retorna `401/403`.

O documento autenticado retorna HTTP `200`, `Cache-Control: no-store` e
`format_version=1`; consumidores devem ler `status` e os códigos por componente.
Use `/ready` para probes HTTP de prontidão. `APP_COMMIT_SHA` é opcional, informado
pelo deploy (40 caracteres hexadecimais); sem ele, `version.commit=null`.

O diagnóstico é somente leitura: timeout de 2s por observação e 1,5s por consulta
PostgreSQL; schema diferente de `0005` impede consultas às tabelas da aplicação.
Tasks, Meta e credenciais são verificações locais de configuração, não provas de
acesso remoto (`remote_checked=false`). Não cria tasks, não envia mensagens nem
consulta Meta/Secret Manager. A validação OIDC pode consultar certificados públicos
Google, como nos workers existentes. A integração de credenciais suporta
referências versionadas no Secret Manager, mas o diagnóstico permanece local:
elas são apenas contabilizadas, nunca retornadas nem acessadas remotamente por
esse endpoint.

Agregação global, em ordem: falha conhecida de aplicação/banco/schema resulta em
`error`; checagem essencial indeterminada, `unknown`; problema opcional/por empresa,
`degraded`; checagem opcional indeterminada, `unknown`; somente o restante é `ok`.
Revisões conhecidas atrás/adiante de `0006` são incompatíveis (`error`); revisão
ilegível/não reconhecida é `unknown`, nunca prontidão `200`. Outbound habilitado
mas mal configurado é `degraded`: bloqueia envios globais, mas não derruba a
ingestão nem elimina a outbox persistida; exige ação operacional. Inbound e outbound
continuam separados. Filas desabilitadas são `ok` com código `*_TASKS_DISABLED`.
Uma conexão antiga desconectada, substituída por outra conexão, é contabilizada
mas não gera alerta de desconexão atual. Falhas recentes de outbound degradam o
diagnóstico; pendências sozinhas não implicam atraso sem um SLA definido.

Atividade usa janela de 24h pela criação/recebimento e até 10.000 registros por
amostra, mais um sentinela para detectar truncamento. CTEs materializadas limitam
as linhas antes de filtros secundários e ordenações. Globalmente, as amostras
usam os índices de data/status; por empresa, o índice de `business_id`, sem ordenar
todo o histórico (`selection=bounded_business`). Não há índice composto
empresa/data: em empresas grandes a amostra não promete conter os eventos mais
recentes. A mesma limitação explícita vale para histórico de conexões/exclusões.
Conexão atual indeterminável por truncamento retorna `unknown`, sem escolher uma
conexão potencialmente errada. Contagens/últimos horários
são limitados à amostra; `truncated=true` sinaliza resultado parcial, não um total
exato. Pendências são amostradas sem corte de idade (`pending_scope=all_ages`),
para não ocultar envios antigos parados. `last_successful_outbound_at` usa `updated_at` de `sent/delivered/read`
(`success_time_source=last_status_update`), não um horário de envio confirmado
pela Meta. Queries usam índices existentes e limites; não há alteração de schema.
Há cinco consultas por diagnóstico global, independentemente do número de empresas,
sem consultas por tenant em loop. `/ready` faz apenas duas observações de até 2s
cada; o limite SQL de 1,5s também protege contra planos caros ou bloqueios.

`DiagnosticsService.business_diagnostics(business_id)` prepara o consumo futuro
pelo SUPER_ADMIN, sem endpoint público por empresa. O chamador deve autorizar o
business antes de invocar o serviço. Retorna somente UUID, estados, contagens e
horários, nunca conteúdo/telefone/identificadores Meta. Webhook processado é métrica
somente global porque a tabela atual não possui `business_id`.

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
etapa do sender. Com a feature desligada, nenhuma configuração `META_*` adicional é exigida
no startup; as credenciais só são validadas quando um envio habilitado é executado.

## Conexões WhatsApp por empresa

A migration `20260902_0005` cria `business_whatsapp_connections`, com no máximo
uma conexão não desconectada por empresa e Phone Number ID único. Os modos
`coexistence` e `api_only` e os estados `pending`, `connected`, `disconnected` e
`error` são domínio explícito; somente `connected` pode enviar. Os campos Meta
legados em `businesses` permanecem durante a transição.

O outbound parte sempre do `business_id` da mensagem, busca a conexão daquela
empresa e constrói o cliente com o Phone Number ID e a versão Graph resolvidos.
O banco guarda apenas `credential_secret_ref`, nunca access token. Conexões novas
resolvem essa referência versionada no Google Secret Manager; o fallback global
continua reservado ao piloto legado descrito abaixo.

O fallback global é exclusivamente `LEGACY/PILOT`: só funciona quando ainda não
há registro em `business_whatsapp_connections` e o
`businesses.meta_phone_number_id` da própria empresa coincide exatamente com
`META_PHONE_NUMBER_ID`. Uma conexão `pending`, `disconnected` ou `error`, uma
credencial ausente ou qualquer inconsistência de empresa falha de forma fechada.
No inbound, o novo modelo conectado tem prioridade; o campo legado é consultado
somente para empresas ainda sem registro novo.

### Embedded Signup em coexistência

O fluxo público autenticado aceita somente empresas `paid` e papéis `owner/admin`.
O PWA recebe apenas App ID, Configuration ID e versão Graph, abre o Facebook Login
for Business com `featureType=whatsapp_business_app_onboarding` e envia ao backend
o código curto e os identificadores devolvidos pela sessão. O backend troca o
código, confirma o WABA e o telefone na Graph API, assina o app no WABA, grava o
token como uma nova versão no Secret Manager e conclui a conexão como
`connected/coexistence`. O número retornado ao PWA é mascarado.

Configure no Cloud Run `META_APP_ID`, `META_EMBEDDED_SIGNUP_CONFIG_ID`,
`META_EMBEDDED_SIGNUP_VERSION` (o sample oficial atual recomenda `v4`) e
`META_GRAPH_VERSION`; mantenha `META_APP_SECRET` exclusivamente no Secret Manager
do serviço e defina `GCP_PROJECT_ID`. A service account do backend precisa de
permissão para criar secrets e adicionar/acessar versões no projeto. No painel da
Meta, o domínio HTTPS do PWA deve estar nos domínios permitidos do JavaScript SDK
e nos OAuth Redirect URIs, a configuração deve ser do WhatsApp Embedded Signup e
o app deve ter as permissões aprovadas `whatsapp_business_management` e
`whatsapp_business_messaging`. O webhook do app deve continuar inscrito em
`messages`; para Coexistence, habilite também os campos oficiais necessários ao
espelhamento, incluindo `smb_message_echoes`.

A `0005` deve ser validada com upgrade/downgrade em PostgreSQL local descartável.
Ela não é aplicada automaticamente no startup e não deve ser aplicada ao
Supabase de produção antes da aprovação operacional específica.

## Autenticação do PWA (16.5B, somente feature branch)

A migration `20260903_0006` acrescenta `users`, `business_user_memberships` e
`auth_sessions`. As migrations 0001–0005 permanecem intactas. Produção ainda está
em 0005: **não mergear/deployar esta branch nem aplicar 0006 sem aprovação**.
O diagnóstico desta branch passa a exigir 0006. Nenhuma migration roda no startup.

Configure `AUTH_JWT_SECRET` com pelo menos 32 bytes aleatórios e
`PWA_ALLOWED_ORIGINS` com origins exatas separadas por vírgula (sem caminho, barra
final ou wildcard). Produção exige HTTPS. Guarde os valores no ambiente seguro,
nunca no Git. Sem configuração válida, somente a nova API fica indisponível (503);
startup, health, webhook e workers existentes continuam independentes de auth.

`/api/v1/auth/login`, `/refresh`, `/logout` e `/active-business` usam JSON; refresh
e logout recebem `{}`. Login normaliza email, verifica Argon2id e retorna JWT HS256
de 10 minutos, com `sub`, `session_id`, `exp` e `jti`. O refresh é aleatório, dura
no máximo 30 dias (prazo absoluto), fica em cookie HttpOnly/SameSite=Lax/Secure em
produção e só seu SHA-256 é armazenado. Cada refresh troca o hash sob lock; replay
é rejeitado. Logout revoga a sessão, invalidando também seus access tokens.
Os parâmetros Argon2id seguem o mínimo [OWASP](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
(19 MiB, duas iterações, paralelismo 1).

O PWA deve usar HTTPS no **mesmo site** da API (ou proxy same-origin) para o cookie
SameSite=Lax funcionar; CORS sozinho não habilita cookies entre sites distintos.
Localmente, use o mesmo hostname, por exemplo `127.0.0.1` em ambas as portas.
Endpoints que alteram cookies exigem Origin permitido; respostas públicas usam
`Cache-Control: no-store`. O access token existe somente em memória no PWA.

`GET /api/v1/me` retorna o próprio perfil e memberships. A empresa operacional
sempre vem da sessão e membership ativa, reconsultadas no PostgreSQL em cada
request. Uma membership é selecionada automaticamente; múltiplas requerem escolha
autorizada. Revogar membership/usuário/sessão tem efeito no próximo request.
`super_admin` é reservado a `/admin`, sem acesso operacional arbitrário por tenant.
`owner/admin` podem planejar onboarding; `attendant/viewer` apenas consultam conexão.
`GET /api/v1/whatsapp/connection` retorna somente estado/modo.
`POST /api/v1/whatsapp/onboarding/plan` reutiliza o serviço 16.4, sem HTTP interno,
conexão Meta ou mutação de dados. Endpoints `/internal/*` continuam com OIDC.

Para provisionar explicitamente o primeiro usuário, após aprovar/aplicar o schema
no ambiente escolhido, use um terminal interativo:

```bash
python -m app.auth.cli --super-admin
python -m app.auth.cli --business-id UUID_DA_EMPRESA --role owner
```

Email é solicitado no terminal; senha (mínimo 12 caracteres) via `getpass`, nunca
argumento, seed ou log. O CLI não sobrescreve usuários existentes. Não há cadastro
público, recuperação de senha, MFA ou OAuth nesta etapa. Antes de exposição pública,
defina também proteção operacional contra abuso de login (limites por origem/IP).
Testes físicos usam exclusivamente `TEST_DATABASE_URL` local descartável e validam
0005 → 0006 → 0005 → 0006; nunca apontar essa variável ao Supabase de produção.
