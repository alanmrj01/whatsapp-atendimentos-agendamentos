# Conversation Robustness Testing — produtividade

## Objetivo

Estressar o motor conversacional determinístico da Alovia com português real de WhatsApp, preservando os workflows existentes. A meta não é tornar o runtime generativo: é aumentar a taxa de entendimento, continuidade e conclusão com regras verificáveis.

O ambiente integrado é produtividade. Produção real não participa dos testes.

## Princípios

1. Descobrir antes de corrigir.
2. Cada gap precisa ser reproduzível por teste.
3. O oráculo é uma propriedade determinística, não a opinião do agente.
4. Não mexer numa etapa saudável para corrigir outra.
5. Correção mínima, regressão ampla.
6. Mass testing não envia WhatsApp real.
7. Smoke por WhatsApp real é separado e usa somente número de teste autorizado.

## Invariantes globais

- pergunta lateral responde e retoma o checkpoint;
- pergunta lateral não preenche o campo pendente;
- correção invalida somente fatos dependentes;
- mudança de serviço não reaproveita cotação/slot incompatível;
- pedido adicional não substitui silenciosamente o pedido atual;
- negação não dispara ação destrutiva;
- pergunta sobre cancelar/reagendar não executa a ação;
- mensagem duplicada não duplica outbound;
- desconhecido não entra em loop;
- reformulação válida recupera o fluxo;
- serviço/preço/duração/disponibilidade nunca são inventados;
- handoff não acontece apenas por informalidade;
- tom permanece educado, humano e levemente formal;
- gíria do cliente pode ser compreendida sem ser espelhada de forma caricata;
- estado e contexto permanecem tenant-scoped.

## Divisão recomendada de agentes

### Agente A — abertura e identificação de serviço

Estados principais: START e BOOKING_SERVICE.

Cobrir: sem saudação, saudação + pedido, gíria, regionalismo, typo, abreviação, caixa alta, emoji, sintoma direto, pedido de orçamento, compra de aparelho versus instalação, serviço configurado no catálogo.

### Agente B — equipamento, quantidade e qualificação

Cobrir checkpoints de modelo, propriedade do equipamento, quantidade, perfil do ambiente, restrições e informações técnicas permitidas.

Verificar que comentários laterais não viram modelo/quantidade e que o motor não inventa especificações.

### Agente C — endereço, imóvel e acesso

Cobrir endereço, cidade, tipo de imóvel, condomínio/prédio, acesso e limitações.

Injetar perguntas de preço/duração/serviço em cada checkpoint e exigir resposta + retomada do campo correto.

### Agente D — disponibilidade, data e horário

Cobrir disponibilidade, dia da semana, data, horário e slot indisponível.

Variar “amanhã”, dia da semana, horários digitados, abreviações, perguntas laterais e correção de data/hora.

### Agente E — correções, contradições e múltiplas intenções

Cobrir “não é X, é Y”, “também quero”, mudança de serviço, correção de quantidade/endereço e mensagens compostas.

Verificar `context_policy`: somente fatos dependentes devem ser invalidados.

### Agente F — preço, orçamento e dúvidas laterais

Cobrir preço, orçamento máximo, duração, o que inclui, visita, adicionais e perguntas técnicas permitidas.

Nunca aceitar número inventado. Quando a fonte não existe, exigir resposta segura/política existente.

### Agente G — confirmação, pós-agendamento, reagendamento e cancelamento

Cobrir confirmação, volta, cancelamento, reagendamento, perguntas sobre possibilidade de cancelar/reagendar versus ação efetiva e novo atendimento após conclusão.

### Agente H — anti-loop, fallback, handoff e tom

Cobrir desconhecido repetido, mídia não suportada, áudio conforme política atual, botões antigos, duplicatas, mensagens sociais consecutivas, interrupções e retomada.

Auditar tom: humano, educado, simples, levemente formal, sem gíria espelhada em excesso.

## Corpus mínimo por agente

Na descoberta, cada agente deve produzir um corpus fixo e revisável de aproximadamente 25 a 60 casos, conforme a amplitude da faixa. Priorizar diversidade sem inflar sinônimos equivalentes.

Para evitar conflito entre agentes, cada agente deve criar seus casos em um arquivo exclusivo dentro de `tests/conversation_stress/` (por exemplo `test_a_openers.py`, `test_b_equipment.py`). Não editar o arquivo de outro agente durante a rodada de descoberta.

Cada família deve incluir, quando aplicável:
- forma canônica;
- sem acento;
- erro de digitação plausível;
- abreviação;
- mensagem curta;
- mensagem longa;
- saudação + intenção;
- gíria/regionalismo + intenção;
- intenção + pergunta lateral;
- negação;
- correção;
- duas intenções na mesma mensagem;
- texto irrelevante que não pode gerar falso positivo.

## Métricas da rodada

Registrar:
- total de casos;
- interpretação correta;
- retomada correta de checkpoint;
- fallback;
- handoff;
- falso positivo de intenção;
- perda de contexto;
- loops/repetições;
- ações destrutivas indevidas;
- conclusão do fluxo quando o cenário é completável.

CTR ou métricas de marketing não pertencem a esta suíte.

## Processo de um gap

1. Reproduzir no baseline da branch de produtividade.
2. Criar teste com nome descritivo.
3. Confirmar que o teste falha pelo motivo esperado.
4. Não alterar runtime em modo de descoberta.
5. Reportar hipótese de causa e arquivos prováveis.
6. Centralizar gaps da mesma família.
7. Autorizar correção em lote pequeno.
8. Aplicar mudança mínima.
9. Rodar regressão completa.
10. Somente depois empacotar candidato para `whatsapp-backend-productivity`.
11. Smoke de produtividade.
12. Produção real somente após auditoria de diff e autorização.

## Arquivos preferenciais em correções autorizadas

Prioridade:
- `app/conversations/interpreter.py`;
- `app/conversations/service_semantics.py`;
- `app/conversations/context_policy.py`;
- `app/conversations/transitions.py`;
- `app/conversations/dialogue.py`;
- testes relacionados.

Evitar tocar em booking, billing, Meta, banco, migrations ou PWA para corrigir linguagem.

## Comandos locais/CI

Baseline rápido:

```bash
pytest tests/test_conversation_robustness.py tests/conversation_stress tests/test_conversation_interpreter.py tests/test_conversation_engine.py -q -p no:cacheprovider
```

Workflow manual:

`Conversation robustness - productivity`

Esse workflow usa dados sintéticos e não possui secrets de Meta/WhatsApp.

## Critério para avançar à produção

Uma família só pode ser considerada pronta quando:
- todos os gaps aprovados têm regressão;
- corpus da família está verde;
- suíte existente do motor está verde;
- nenhuma regressão de contexto/loop/handoff foi introduzida;
- diff está limitado ao escopo aprovado;
- candidato em produtividade passa health/ready e smoke;
- a revisão e o SHA realmente testados são os mesmos que serão promovidos.
