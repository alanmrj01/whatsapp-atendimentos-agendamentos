# Instruções do projeto

## Versionamento e commits automáticos

- Este projeto está vinculado ao seguinte repositório:
  [`COLE_AQUI_A_URL_DO_REPOSITORIO`](https://github.com/alanmrj01/whatsapp-atendimentos-agendamentos)
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
- Ao finalizar, informe:
  - resumo das alterações;
  - testes executados e resultados;
  - branch utilizada;
  - mensagem e hash do commit;
  - resultado do push.

Considere uma alteração aprovada quando eu disser expressamente que está aprovada, concluída, correta ou solicitar sua entrega final. Durante ajustes intermediários, não faça commit sem que eu peça.
