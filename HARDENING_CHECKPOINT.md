# Checkpoint do hardening conversacional

- HEAD atual: `7016c41e35ef2e3a8615b84bf8492ab7e550ec38`
- Último commit: `7016c41 feat: fotos e preços no catálogo de equipamentos (#34)`
- Arquivos alterados: `app/conversations/constants.py`, `app/conversations/context_policy.py`, `app/conversations/facts.py`, `app/conversations/interpreter.py`, `app/conversations/outbound.py`, `app/conversations/transitions.py`, `app/repositories/conversations.py`, `app/repositories/outbound_tasks.py`, `tests/test_conversation_engine.py`, `tests/test_conversation_interpreter.py`, `tests/test_conversation_repository.py`, `tests/test_equipment_recommendation.py`, `tests/test_outbound_tasks.py`.
- Testes executados: direcionados prioritários, `180 passed`; conjunto direcionado ampliado, `185 passed`; operacional, `7 passed, 3 skipped`; suíte completa anterior à última correção, `634 passed, 66 skipped, 7 failed`, com as mesmas sete falhas preexistentes do baseline.
- Concluído: semântica de negação/correção; perguntas laterais e respostas sociais sem consumir tentativas; confirmação de botão antigo; `purchase_mode`; compra simples e compra com instalação; endereços de entrega e serviço separados; recomendação única; imagem opcional não bloqueia sequência; pedido adicional não substitui o atual; helpers completos; nenhuma migration alterada.
- Pendências exatas: repetir compile/import/diff-check; auditar secrets e migrations; repetir suíte completa após a última correção; remover este arquivo; commit final e push.
- Próximo comando recomendado: `python -m pytest tests -q -p no:cacheprovider` com `PYTHONDONTWRITEBYTECODE=1`.
