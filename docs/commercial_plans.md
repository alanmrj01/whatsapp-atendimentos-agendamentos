# Planos comerciais da ALOVIA

## Regra de prioridade

1. `admin_full_access=true`: acesso administrativo total e ilimitado.
2. Assinatura comercial válida: aplica o plano contratado.
3. Conta que já teve operação real, mas perdeu o direito ativo:
   `payment_blocked`, somente histórico preservado/leitura e WhatsApp pausado.
4. Conta que nunca teve operação real: `demo`, podendo mostrar exemplos
   explicitamente identificados como demonstração.

`has_had_operational_access` é permanente. Depois que uma empresa começa a operar
com dados reais, dados de demonstração jamais devem reaparecer.

## Basic — lançamento

- R$ 197,00 por mês.
- 1 usuário.
- Até 500 mensagens enviadas pela Alovia por mês.
- Mensagens recebidas do cliente e reações não consomem a franquia.
- Ao atingir a franquia, novos envios ficam pausados até a renovação mensal da
  franquia ou até um upgrade elegível para o Plus.

A franquia é aplicada no servidor antes do envio ao provedor. Contas com
`admin_full_access=true` e acessos legados preservados não recebem este limite.

## Plus

- R$ 397,00 por mês.
- Até 5 usuários.
- Até 900 mensagens enviadas pela Alovia por mês.
- Mensagens recebidas do cliente e reações não consomem a franquia.
- Ao atingir a franquia, novos envios ficam pausados até a renovação mensal da
  franquia.

O Plus é parte ativa da oferta comercial. `BILLING_PLUS_ENABLED=false` fica
disponível apenas como kill switch de emergência.

### Upgrade Basic → Plus

- Assinaturas mensais recorrentes por cartão ou Pix Automático podem migrar
  para Plus sem criar uma segunda assinatura.
- A franquia sobe para 900 assim que o provedor confirma a atualização; o valor
  de R$ 397,00 passa a valer nas cobranças pendentes/futuras do ciclo recorrente.
- Pix comum e ciclos trimestral/anual não são alterados no meio do período:
  nesses casos o cliente aguarda a renovação da franquia ou troca de plano na
  renovação.

## Liberação administrativa

Uma empresa liberada pelo SUPER_ADMIN recebe todas as funcionalidades presentes e
futuras dos planos comerciais, técnicos ilimitados e mensagens ilimitadas enquanto
o override estiver ativo.

## Inadimplência

`PAYMENT_OVERDUE` coloca a assinatura em `past_due`. `past_due` não concede
entitlement operacional.

A empresa continua podendo acessar seus registros históricos, mas mutações e
automação ficam bloqueadas. O vínculo Meta/WhatsApp não é apagado nem
desconectado: inbound e outbound operacionais são pausados por entitlement.

Quando um pagamento válido é confirmado/recebido, a assinatura retorna a
`active`, o entitlement volta a ficar ativo e a conexão já cadastrada volta a ser
utilizada automaticamente.

Uma assinatura cancelada conserva acesso apenas até o `access_until` já pago.


## Compatibilidade de acessos anteriores ao billing comercial

Registros históricos com `access_mode=paid` permanecem operacionalmente ativos
para não interromper tenants/pilotos existentes, mas isso não equivale a
`admin_full_access`.

Esses registros não recebem automaticamente funcionalidades exclusivas do Plus,
mensagens ilimitadas ou técnicos ilimitados. O acesso administrativo total só é
ativado por uma ação explícita do SUPER_ADMIN.


## Pix Automático em Produção

O Pix Automático permanece implementado e validado em Sandbox, mas deve ficar
desabilitado em Produção enquanto a conta Asaas não estiver elegível para o
recurso.

Durante esse período, o lançamento comercial utiliza cartão recorrente.
`BILLING_PIX_AUTOMATIC_ENABLED=false` mantém o backend fail-closed.

Quando o Asaas liberar o recurso, a ativação exige:
1. cadastrar no webhook os eventos de Pix Automático;
2. definir `BILLING_PIX_AUTOMATIC_ENABLED=true`;
3. validar novamente o fluxo real antes de disponibilizá-lo na interface.

## Checkout nativo ALOVIA

O checkout nativo de cartão é controlado por `BILLING_NATIVE_CARD_CHECKOUT_ENABLED`.
O padrão é `false`, preservando o checkout hospedado pelo Asaas até que o fluxo
nativo tenha sido validado em Sandbox.

Quando habilitado, a sessão de checkout expira em 10 minutos. O navegador envia
os dados do cartão apenas no momento da confirmação, sob HTTPS; número completo
do cartão e código de segurança não devem ser persistidos em banco, logs,
telemetria ou ferramentas de suporte.

O backend cria a assinatura recorrente diretamente no Asaas e usa Webhooks de
cobrança como fonte de verdade para liberar o acesso. Em caso de timeout do
provedor, a integração tenta reconciliar a assinatura pelo `externalReference`
antes de permitir qualquer nova tentativa, reduzindo risco de duplicidade.

## Compra durante liberação administrativa

Enquanto `admin_full_access=true`, a empresa já possui acesso operacional completo
concedido pelo administrador do ALOVIA e não deve iniciar uma nova cobrança de
cartão. O backend rejeita a tentativa antes de qualquer chamada ao provedor.

Ao revogar a liberação administrativa, `admin_full_access=false`. Se não houver
uma assinatura comercial válida, a conta volta ao estado comercial aplicável e
pode contratar normalmente. Uma assinatura comercial real já existente continua
sendo respeitada independentemente da liberação administrativa.
