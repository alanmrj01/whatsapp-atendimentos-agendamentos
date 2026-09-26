# Especificação mestre do motor conversacional determinístico da Alovia

## 1. Objetivos do motor

O motor atende empresas de refrigeração por regras determinísticas, sem usar IA
generativa para decidir preço, duração, rota, disponibilidade ou confirmação. A
próxima ação deriva de intenção, fatos conhecidos, fatos novos, correções,
perguntas laterais e fatos ainda ausentes. Recomendações são referências
comerciais iniciais, nunca laudos de engenharia.

## 2. Princípios de UX

- Fazer uma pergunta útil por vez.
- Aproveitar fatos antecipados e não perguntar novamente o que já foi confirmado.
- Registrar fatos silenciosamente, sem repetir “anotei” a cada resposta.
- Responder perguntas laterais e retomar o ponto correto.
- Usar linguagem curta e explicar limites sem códigos internos.
- Não criar timer de três minutos; usar somente o debounce curto existente.

## 3. Modelo mental

`intenção + fatos persistidos + fatos novos + correções + pergunta lateral + fatos faltantes → próxima ação útil`.

O estado canônico indica a ação em curso, mas não é a única fonte de verdade. Os
extratores globais rodam antes do handler, permitindo fatos fora da ordem.

## 4. Fatos persistidos

O `context` mantém intenção, serviço, modo cotação/agendamento, quantidade,
endereço, acesso, imóvel, horários do local, responsável, telefone, aparelho,
modelo, altura, área, ocupação, preferência, ciclo, restrições físicas,
recomendação, data/horário e evidência segura de mídia.

## 5. Confiança, origem e correção

A extração global é conservadora. Respostas curtas como “sim” dependem da pergunta
ativa. A origem é conceitualmente mensagem livre, ação interativa ou configuração
empresarial; o contexto guarda o valor operacional atual, não a transcrição. Uma
correção explícita substitui o fato anterior e invalida derivados dependentes.

## 6. Intenções

Incluem saudação, serviço, cotação, compra de equipamento, agendamento,
disponibilidade, preço, duração, reagendamento, cancelamento e atendimento humano.
Uma mensagem pode conter mais de uma intenção; decisão explícita e contexto
definem a precedência.

## 7. Escopos de atendimento

O motor cobre serviços ativos configurados pela empresa. Fora disso, oferece
opções compatíveis ou equipe humana. Nunca cria serviço, preço ou política ausente.

## 8. Contratação de serviços

O serviço precisa estar ativo e compatível com automação. Quantidade, acesso,
endereço e qualificadores são solicitados somente quando o intake exigir. A
confirmação final revalida regras e disponibilidade sob transação.

## 9. Cotação

Cotação não é agendamento. Pode incluir serviço, equipamento ou ambos. “Só queria
a cotação” conclui sem repetir preço e sem criar appointment. Equipamento,
instalação, materiais e adicionais aparecem separados; preço ausente não vira zero.

## 10. Instalação

O fluxo identifica se o cliente já possui aparelho. Para aparelho existente,
coleta modelo/BTU. Para recomendação, coleta perfil seguro. Altura, imóvel, acesso,
endereço, materiais e agenda seguem as regras cadastradas.

## 11. Limpeza e higienização

Modelo/capacidade e quantidade são coletados quando afetam preço ou duração.
Aparelhos diferentes não são tratados como esforço idêntico por suposição.

## 12. Manutenção

Relato de defeito é contexto operacional, não diagnóstico. O motor coleta o
necessário e agenda quando permitido, sem afirmar causa, peça ou reparo.

## 13. Recarga e diagnóstico

Pedido de carga de gás não prova falta de fluido. Vazamento, pressão e necessidade
de recarga dependem de avaliação técnica e do serviço configurado.

## 14. Venda de equipamento

Compra sem instalação usa o perfil de recomendação, mas não cria agendamento.
Indicação e preço aparecem separados; estoque e valor ausentes vão para confirmação
comercial, preservando os fatos no handoff.

## 15. Recomendação de equipamento

Considera área, ocupação, preferência, ciclo, restrições físicas, capacidade e
itens ativos da empresa. A frase é “uma boa referência inicial”, nunca “o ideal”.
Insolação, pé-direito, carga de equipamentos e uso comercial podem exigir validação.

## 16. Ciclo frio/quente-frio

`equipment_cycle` aceita `cooling_only` e `heat_cool`. “Só frio”, “quente e frio”
e “ciclo reverso” são reconhecidos. Só se seleciona item que declare o ciclo.

## 17. Restrições físicas

Espaço interno e externo são fatos distintos. Descrições qualitativas e dimensões
exatas são aceitas. `ample` pode seguir; `limited`, `technical_balcony` ou
`measured` sem compatibilidade verificável falham fechado. Forma da condensadora
só é usada quando declarada no catálogo; nunca se presume encaixe.

## 18. Catálogo empresarial

Há 30 configurações atuais de referência, não um ranking auditado. Conta paga
recebe presets próprios. A empresa pode visualizar, editar dados comerciais,
ativar, desativar ou remover. Preset removido fica inativo e não reaparece. O
recomendador usa somente itens ativos daquele `business_id`; campos técnicos
incertos permanecem nulos.

## 19. Mídia

- Imagem inbound: identificador, MIME, legenda limitada e fato de recebimento;
  nunca payload bruto/binário.
- Imagem outbound: URL HTTPS previamente cadastrada e verificada.
- Áudio/vídeo: não interpretar; oferecer texto ou equipe.
- Continuar por texto ou fazer handoff preserva estado e fatos.

## 20. Mensagens curtas

Outbound textual deve ter aproximadamente quatro linhas semânticas. Uma função
central divide por quebras e sentenças; botões/lista ficam somente no último bloco.
A largura real varia por aparelho, logo o limite de caracteres é conservador.

## 21. Perguntas laterais

Preço, duração ou conteúdo do serviço são respondidos com dados cadastrados; o
fluxo retoma o fato ausente. Encerrar cotação não é nova pergunta de preço.

## 22. Endereço e localidade

Endereço deve permitir rota segura. Sem cidade, confirma-se o município. Correção
explícita substitui a cidade e invalida slots da rota anterior. Geocodificação
ambígua nunca escolhe outro município silenciosamente.

## 23. Agenda

Datas vêm do PostgreSQL e respeitam expediente, técnico, duração, deslocamento,
bloqueios, prédio e compromissos. A confirmação é atômica; horário exibido não é
reserva até o commit.

## 24. Reagendamento

Seleciona appointment do mesmo cliente/business, calcula novas opções e confirma
atomicamente. Slot ocupado não cancela o compromisso anterior.

## 25. Cancelamento

Seleciona appointment próprio, pede confirmação e é idempotente. “Manter” não
altera registro. Histórico não é apagado.

## 26. Handoff

Ocorre por pedido explícito, política empresarial, configuração ausente ou limite
técnico real. Motivo é sanitizado e o contexto coletado é preservado.

## 27. Respostas incompletas

Resposta inválida recebe reformulação curta. Após o limite de reparo, oferece-se
equipe. Um fato antecipado válido não conta como falha da pergunta corrente.

## 28. Respostas antecipadas

Extratores globais tratam área, pessoas, ciclo, preferência, imóvel, horário,
responsável, telefone, modelo e altura antes do handler. Vários fatos claros são
salvos juntos e só o próximo ausente é perguntado.

## 29. Correções

O último valor explícito substitui pessoas, área, ciclo, preferência, modelo,
quantidade, horário, responsável e cidade. Mudança de endereço remove seleção de
agenda dependente da rota antiga.

## 30. Mensagens contraditórias

Sem marcador de correção, fatos incompatíveis geram confirmação. Com “corrigindo”,
“desculpa” ou “na verdade”, prevalece o trecho recente extraído com segurança.

## 31. Fora do escopo

Diagnóstico por mídia, cálculo térmico completo, decisão de engenharia, preço não
cadastrado e interpretação livre sem regra. O limite é informado e há continuidade
segura por texto ou equipe.

## 32. Pós-atendimento

Estados e status de mensagem são preservados. O motor informa conclusão e pode
voltar ao menu, mas não fabrica garantia, pesquisa ou novo serviço.

## 33. Segurança

Isolamento por `business_id`; assinatura e idempotência de webhook; grupos, canais,
comunidades e broadcasts bloqueados. Logs não contêm telefone, mensagem, token ou
payload sensível. Tasks levam identificadores e outbox respeita supressões.

## 34. Critérios para não inventar

Não inventar preço, estoque, especificação, compatibilidade, imagem, rota, duração,
disponibilidade, diagnóstico ou elegibilidade. Dado sem fonte fica nulo. Se a
decisão depender dele, perguntar ou falhar fechado.

## 35. Matriz de cenários de teste

| ID | Cenário | Resultado esperado |
|---:|---|---|
| 01 | Saudação sem pedido | Saudação curta e opções úteis |
| 02 | Nome e serviço na primeira mensagem | Salva ambos sem repetir |
| 03 | Pedido natural de limpeza | Seleciona serviço ativo compatível |
| 04 | Pedido natural de instalação | Inicia intake correto |
| 05 | Pedido de manutenção | Não diagnostica; coleta o necessário |
| 06 | Pedido de carga de gás | Trata como serviço/diagnóstico |
| 07 | Serviço inexistente | Não inventa; oferece opções/equipe |
| 08 | Pergunta de preço no menu | Responde dado cadastrado |
| 09 | Pergunta de duração durante fluxo | Responde e retoma contexto |
| 10 | Side question sem preço | Informa avaliação necessária |
| 11 | Cotação de instalação | Não cria appointment |
| 12 | “Só queria a cotação” | Não repete preço |
| 13 | Cotação decide agendar | Abre agenda mantendo fatos |
| 14 | Compra apenas do aparelho | Sem agenda; handoff comercial |
| 15 | Equipamento + instalação | Preços separados |
| 16 | Aparelho existente | Coleta modelo/BTU |
| 17 | Cliente não sabe modelo | Inicia recomendação |
| 18 | Área/pessoas fragmentadas | Persiste e pergunta próximo fato |
| 19 | Todos os fatos em uma mensagem | Pula perguntas redundantes |
| 20 | Fatos fora de ordem | Usa fatos antecipados |
| 21 | Correção de pessoas | Último valor prevalece |
| 22 | Correção de área | Nova área prevalece |
| 23 | Correção de ciclo | Novo ciclo prevalece |
| 24 | Correção de preferência | Novo perfil prevalece |
| 25 | Correção de modelo | Novo modelo prevalece |
| 26 | Somente frio | Filtra `cooling_only` |
| 27 | Quente/frio | Filtra `heat_cool` |
| 28 | Ciclo incompatível | Handoff seguro com contexto |
| 29 | Espaço interno amplo | Perfil interno satisfeito |
| 30 | Espaço interno apertado | Avaliação humana |
| 31 | Espaço externo amplo | Perfil externo satisfeito |
| 32 | Varanda técnica pequena | Não recomenda automaticamente |
| 33 | Medida exata interna | Aceita sem alegar encaixe |
| 34 | Medida exata externa | Aceita sem alegar encaixe |
| 35 | Condensadora compacta | Só item que declare compatibilidade |
| 36 | Forma sem metadata | Falha fechado |
| 37 | Piloto sem presets | Fallback legado controlado |
| 38 | Catálogo empresarial ativo | Só IDs ativos |
| 39 | Modelo desativado | Não é selecionado |
| 40 | Modelo removido | Não reaparece silenciosamente |
| 41 | Catálogo vazio | Handoff, não catálogo global |
| 42 | Conta paga criada | Recebe 30 presets |
| 43 | Conta gratuita | Não recebe operação paga |
| 44 | Conversão para paid | Cria presets ausentes |
| 45 | Preço de aparelho cadastrado | Exibe como equipamento |
| 46 | Preço de aparelho nulo | Não inventa valor |
| 47 | Preço de instalação | Rótulo separado |
| 48 | Material adicional | Aviso separado |
| 49 | Imagem oficial cadastrada | Outbound image |
| 50 | Sem imagem verificada | Não usa foto genérica |
| 51 | URL não HTTPS | Rejeita antes da Meta |
| 52 | Foto inbound com legenda | Metadata segura e legenda limitada |
| 53 | Foto sem legenda | Confirma, sem diagnóstico |
| 54 | Foto com SHA/binário | Campo bruto não persiste |
| 55 | Áudio inbound | Texto ou equipe |
| 56 | Vídeo inbound | Texto ou equipe |
| 57 | Continuar por texto | Conserva estado/fatos |
| 58 | Handoff após mídia | Conserva contexto |
| 59 | Texto longo | Divide em até quatro linhas lógicas |
| 60 | Interactive longo | Botões apenas no último bloco |
| 61 | Lista longa | Payload íntegro |
| 62 | Webhook repetido | Sem reprocessamento |
| 63 | Outbox repetida | Sem novo envio local |
| 64 | Tasks concorrentes | Bloqueio local |
| 65 | Grupo/comunidade | Ignorado antes do motor |
| 66 | Canal/newsletter/broadcast | Ignorado antes do motor |
| 67 | Exclusão fixa | Sem outbound automático |
| 68 | Janela humana ativa | Bot suprimido |
| 69 | Janela humana expirada | Bot pode retomar |
| 70 | Endereço completo | Segue para rota |
| 71 | Endereço sem cidade | Confirma município |
| 72 | Corrige cidade | Substitui e invalida slot |
| 73 | Rota indisponível | Não inventa minutos |
| 74 | Prédio 08h–17h, serviço 3h | Último início 14h |
| 75 | Altura acima de 3m | `work_at_height=true` |
| 76 | Tubulação desconhecida | Disclaimer, sem exigir metros |
| 77 | Esposa Juliana no local | Salva e não repete pergunta |
| 78 | Outro telefone | Mostra na confirmação |
| 79 | Reagendamento com conflito | Preserva compromisso anterior |
| 80 | Cancelamento repetido | Idempotência e histórico |

Testes unitários cobrem regras e contratos; PostgreSQL cobre constraints,
concorrência, agenda e migrations; webhook/outbox cobrem assinatura e idempotência.
