# Serviço planejado após atendimento humano

Esta etapa não implementa painel, cobrança nem um novo fluxo conversacional. O
catálogo em `data/default_service_catalog.json` é apenas uma origem versionada
para onboarding. Uma cópia inicial pertence ao `business` e nunca é sincronizada
ou sobrescrita automaticamente quando o arquivo mudar.

## Contrato futuro do painel

Um orçamento que exija atendimento humano deverá ser persistido futuramente em
uma entidade própria e multiempresa, sem usar `conversation.context` como banco.
Essa entidade deverá vincular `business`, `customer` e, quando aplicável, o
serviço-base, além de registrar:

- duração planejada aprovada;
- preço aprovado e moeda;
- requisitos estruturados do atendimento;
- endereço e restrição de horário aplicáveis;
- status, validade, autor e trilha de auditoria.

Somente um planejamento aprovado poderá ser oferecido à automação para
agendamento. A confirmação continuará copiando duração, preço, deslocamento,
endereço e regras para os snapshots imutáveis do `appointment`; o catálogo
comercial global não será alterado para acomodar um orçamento individual.

## Limites preservados

O painel futuro deverá aplicar isolamento por `business_id`, permissões e
auditoria. Planos, assinatura, pagamentos, tolerância e suspensão serão módulos
separados e não entrarão no `ConversationEngine`. Nenhuma lógica de pagamento ou
gestão de técnico é criada nesta etapa.
