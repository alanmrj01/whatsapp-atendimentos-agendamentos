# Instruções do projeto

## Versionamento e commits automáticos

- Este projeto está vinculado ao repositório https://github.com/alanmrj01/whatsapp-atendimentos-agendamentos.
- Sempre que eu aprovar uma alteração como concluída, faça automaticamente o versionamento completo, sem precisar esperar que eu peça novamente.
- Antes do commit, execute os testes relevantes disponíveis no projeto.
- Confira `git status` e revise o diff para garantir que somente as alterações relacionadas à tarefa sejam incluídas.
- Não inclua alterações anteriores, arquivos do usuário ou modificações não relacionadas à tarefa.
- Nunca inclua `.env`, credenciais, senhas, tokens, chaves de API, bancos locais, arquivos temporários, dependências instaladas ou artefatos de build.
- Atualize o `.gitignore` quando necessário para impedir o envio desses arquivos.
- Crie um commit com uma mensagem clara, curta e específica sobre a alteração realizada.
- Depois do commit, faça automaticamente o push para o repositório remoto configurado.
- Use a branch atualmente definida para o trabalho. Nunca troque de branch, envie diretamente para outra branch ou faça merge sem autorização.
- Nunca reescreva o histórico Git, nunca use push forçado e nunca apague commits ou alterações existentes.
- Se houver conflito, falha nos testes, erro de autenticação, ausência do repositório remoto ou qualquer risco de sobrescrever trabalho existente, não force a operação. Pare e explique exatamente o que preciso resolver.
- Se os testes falharem por um problema anterior e não relacionado à alteração, informe o problema antes de decidir sobre o commit.
- Ao finalizar, informe: resumo das alterações; testes executados e resultados; branch utilizada; mensagem e hash do commit; resultado do push.
- Considere uma alteração aprovada quando eu disser expressamente que está aprovada, concluída, correta ou solicitar sua entrega final. Durante ajustes intermediários, não faça commit sem que eu peça.

## Ambiente de produtividade e isolamento

- O ambiente de produtividade é o único ambiente permitido para validações integradas desta frente. Ele é paralelo à produção real.
- Backend de produtividade: `whatsapp-backend-productivity`.
- PWA de produtividade: `https://alovia-productivity.netlify.app`.
- A produção real `whatsapp-backend` e `https://alovia.netlify.app` são estritamente proibidas para stress tests.
- Nunca consultar, alterar, migrar, reiniciar, redirecionar tráfego ou disparar mensagens usando recursos de produção real.
- Nunca reutilizar as filas de produção `whatsapp-events` ou `whatsapp-outbound` em testes de produtividade.
- Nunca usar secrets de produção. Em produtividade, usar somente recursos explicitamente identificados como `alovia-productivity-*` ou outro recurso de teste aprovado.
- Nunca usar clientes, telefones, mensagens, credenciais ou dados pessoais reais no corpus automatizado.
- Stress tests não enviam WhatsApp real. Smoke test por WhatsApp real só pode ocorrer quando a tarefa autorizar explicitamente um número de teste e a integração de produtividade estiver isolada.
- Se qualquer configuração apontar para produção real, interromper a tarefa e reportar a evidência. Não tentar “corrigir” automaticamente infraestrutura de produção.

## Motor conversacional Alovia — contrato de produto

- O runtime deve permanecer determinístico. Não introduzir LLM, IA generativa, prompt remoto, classificação probabilística externa ou resposta gerada por modelo no atendimento.
- O objetivo é ampliar entendimento, recuperação e naturalidade sem redesenhar os workflows já validados.
- Não alterar booking, catálogo, preços, disponibilidade, billing, planos, regras financeiras ou dados de empresa para corrigir interpretação de linguagem.
- Não inventar preço, prazo, disponibilidade, medida, especificação técnica, causa de defeito, serviço, política ou informação que não esteja disponível no contexto/catálogo/configuração.
- Uma interpretação incerta não pode virar fato. Perguntar apenas o mínimo necessário para desambiguar ou aplicar a política de fallback/handoff existente.
- A mudança de linguagem do cliente não pode alterar silenciosamente regra de negócio.

## Tom de atendimento

- A Alovia representa uma empresa. O tom deve ser humano, educado, simples e levemente formal.
- Ser cordial sem ser burocrático. Preferir frases claras e curtas; não usar palavras difíceis quando uma expressão cotidiana resolve.
- Mesmo que o cliente use “mano”, “brother”, “véi”, “parça”, “oxe”, “uai”, “blz” ou linguagem muito informal, entender a intenção sem imitar gírias marcadas.
- Não responder com intimidade artificial, excesso de emoji, memes, ironia, sarcasmo ou linguagem juvenil forçada.
- Pode acompanhar naturalmente uma saudação ou um “tudo bem”, mas deve preservar postura profissional.
- Não transformar toda mensagem em roteiro robótico. Se a intenção já estiver clara, avançar para o próximo dado necessário em vez de pedir confirmação redundante.

## Perguntas laterais e retomada do workflow

- Uma pergunta lateral deve ser respondida de forma humana, objetiva e profissional e, na mesma resposta sempre que possível, retomar o checkpoint exato que estava pendente.
- Exemplo de contrato: se o sistema aguardava endereço e o cliente pergunta “quanto tempo demora?”, responder o tempo somente se conhecido e em seguida voltar a pedir o endereço; não abrir menu, não reiniciar o serviço e não perder contexto.
- A pergunta lateral não pode ser salva como resposta do campo pendente.
- Depois de responder a lateral, estado, serviço, dados válidos e checkpoint devem permanecer coerentes.
- Duas ou mais perguntas laterais consecutivas não justificam sozinhas handoff, reinício ou repetição em loop.
- A retomada deve evitar repetir blocos grandes já enviados; repetir somente o necessário para continuar.

## Correções, contradições e mudança de intenção

- Correções explícitas do cliente têm prioridade sobre afirmações anteriores: “não é limpeza, é manutenção” deve considerar manutenção como intenção atual.
- Ao corrigir um fato, invalidar apenas fatos derivados que dependem dele. Preservar dados seguros e independentes já coletados.
- Troca de serviço não deve carregar orçamento, cotação, duração ou slot que pertença ao serviço anterior.
- Dados reutilizáveis seguros, como endereço já confirmado quando a política permitir, não devem ser apagados sem necessidade.
- “Também quero...” representa pedido adicional; não substituir silenciosamente o pedido ativo.
- Perguntas sobre cancelar/reagendar não são ações destrutivas. Só executar cancelamento/reagendamento quando a intenção de ação estiver inequívoca.
- Negação sempre deve ser respeitada. “Não quero cancelar” nunca pode acionar cancelamento.

## Informalidade, ruído e português real

- Testar abreviações, erros de digitação, ausência de acentos, pontuação, caixa alta/baixa, mensagens curtas, regionalismos, gírias, emojis, discurso direto e frases com mais de uma intenção.
- Regionalismo é variação linguística, não dado de localização do cliente. Nunca inferir região, origem ou perfil pessoal a partir de uma expressão.
- Não resolver robustez adicionando uma lista infinita de frases sem analisar a semântica. Prioridade: normalização -> intenção/semântica -> política de interrupção -> retomada do checkpoint.
- Aliases devem respeitar limite de palavra/contexto para evitar falsos positivos em termos não relacionados à refrigeração.
- Serviço só pode ser resolvido contra a intenção semântica e o catálogo/configuração da empresa.

## Anti-loop, fallback e handoff

- Nenhuma alteração pode aumentar loops, repetição de fallback ou repetição da mesma pergunta sem progresso.
- Entrada desconhecida deve seguir política limitada e observável. Nunca responder indefinidamente com “não entendi”.
- Uma reformulação válida deve limpar o estado de retry correspondente e permitir continuidade normal.
- Handoff humano é último recurso conforme política existente, não atalho para linguagem informal.
- Contato bloqueado, conversa em controle humano ou automação desabilitada nunca pode voltar a receber resposta automática por uma alteração desta frente.
- Mensagens duplicadas/idempotentes nunca podem gerar resposta duplicada.

## Testes obrigatórios

- Toda falha real corrigida deve ganhar teste de regressão permanente antes ou junto da correção.
- Testes de descoberta devem ser determinísticos, com corpus fixo e dados sintéticos. Não depender de rede, horário real, geração aleatória não semeada ou serviço externo.
- Cada teste deve declarar a propriedade protegida: intenção, estado, contexto preservado, contexto invalidado, conteúdo factual, anti-loop, idempotência ou handoff.
- Não aceitar teste que apenas confira texto inteiro quando a propriedade importante for estado/contexto; prefira invariantes robustas.
- Testes de tom podem proibir gírias/impropriedades claras, mas não devem congelar toda a copy sem necessidade.
- Antes de propor alteração no runtime, executar ao menos:
  - `tests/test_conversation_interpreter.py`;
  - `tests/test_conversation_engine.py`;
  - `tests/test_conversation_robustness.py`.
- Se a mudança tocar booking/automation/outbound, executar também os testes específicos desses módulos e o workflow de conversação existente.

## Modo de descoberta para agentes Codex

- Por padrão, uma tarefa de stress/descoberta NÃO autoriza alteração em `app/**`.
- Na fase de descoberta, o agente pode adicionar/ajustar somente testes e documentação relacionados ao cenário delegado.
- Primeiro reproduzir o gap em teste. Depois registrar: entrada, estado inicial, comportamento observado, comportamento esperado, risco e arquivo provável responsável.
- Não “consertar” um teste ajustando a expectativa para o comportamento atual quando esse comportamento viola o contrato acima.
- Alteração de runtime só é permitida quando a tarefa disser explicitamente que a correção daquele gap foi autorizada.
- Em correção autorizada, fazer a menor mudança possível e apenas nos arquivos necessários. Refactor oportunista é proibido.
- Mudança inesperada fora de `app/conversations/**` e testes relacionados exige parar e justificar antes de continuar.
- Não alterar migrations, dependências, Dockerfile, billing, tracking, Meta, PWA ou infraestrutura durante uma correção de linguagem, salvo autorização explícita e evidência de necessidade.

## Relatório obrigatório de cada agente

Ao concluir uma rodada de stress, informar:
- faixa do workflow testada;
- número de casos executados;
- casos aprovados;
- gaps reproduzidos;
- falsos positivos descartados;
- estados/checkpoints afetados;
- risco de loop, perda de contexto, handoff indevido ou ação destrutiva;
- testes adicionados;
- arquivos alterados;
- nenhuma alteração de runtime, quando estiver em modo de descoberta.
