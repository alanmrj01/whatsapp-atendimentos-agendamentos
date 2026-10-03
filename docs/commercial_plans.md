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
- Até 2 técnicos.
- Até 800 mensagens por mês.
- Único plano disponível para contratação no lançamento.

Os limites acima são inicialmente informativos na oferta. A aplicação dos limites
de técnicos e mensagens dentro do produto será feita em etapa específica.

## Plus — em breve

- R$ 397,00 por mês.
- Mais de 2 técnicos.
- Até 2.000 mensagens por mês.
- Oferta automática de nova limpeza após 6 meses.
- PMOC após 12 meses: funcionalidade planejada, ainda não implementada.

O checkout Plus permanece bloqueado enquanto `BILLING_PLUS_ENABLED=false`.

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
