# Alovia Conversation Lab

## Objetivo

Executar regressão e discovery massivo do motor conversacional determinístico sem enviar WhatsApp, sem acessar Meta e sem criar/cancelar/reagendar compromissos reais.

O laboratório reaproveita o código de `app/conversations`, mas usa repositório em memória e adapter de booking dry-run.

## Modos

### Regression

Corpus congelado de comportamentos validados. Qualquer gap retorna exit code 1 e bloqueia promoção.

### Discovery

Expande seeds por variações controladas de português real: caixa, acentos, abreviações, gírias leves, pontuação e typos recorrentes. Discovery não altera runtime nem corpus automaticamente. Os casos aprovados são emitidos como `CONVERSATION_LAB_REGRESSION_CANDIDATE`; gaps são emitidos como `CONVERSATION_LAB_GAP`.

A promoção de um caso para regressão só deve ocorrer quando o oráculo determinístico estiver correto. Isso evita congelar um falso positivo como comportamento desejado.

## Segurança

- `CONVERSATION_LAB_ALLOW_EXTERNAL_IO=true` aborta o runner por design.
- Não importa sender Meta nem webhook.
- Repositório é em memória.
- Booking é dry-run; ações destrutivas são registradas, não executadas.
- Não usa DATABASE_URL.
- Não usa credenciais WhatsApp.
- Não usa billing, Pix ou tracking.
- O Job deve ser implantado como serviço separado do backend.

## Execução local

```bash
python -m app.conversation_lab.runner --mode regression
python -m app.conversation_lab.runner --mode discovery --per-seed 120
python -m app.conversation_lab.runner --mode all --per-seed 120
```

## Cloud Run Job

Buildar uma imagem específica usando `Dockerfile.conversation-lab`. Não reutilizar a imagem HTTP do backend.

O Job não precisa de secrets. Variáveis recomendadas:

- `ENVIRONMENT=test`
- `CONVERSATION_LAB_ALLOW_EXTERNAL_IO=false`
- `CONVERSATION_LAB_MODE=regression` ou `discovery`
- `CONVERSATION_LAB_PER_SEED=120`

## Ciclo de melhoria

1. Regression verde.
2. Discovery massivo.
3. Agrupar gaps por família e causa provável.
4. Criar teste determinístico reproduzível para cada gap aprovado.
5. Aplicar correção mínima.
6. Reexecutar regressão completa + discovery focal.
7. Adicionar casos aprovados ao corpus de regressão.
8. Repetir.

Isto é aprendizado cumulativo do corpus, não machine learning estatístico: o motor continua determinístico e auditável.
