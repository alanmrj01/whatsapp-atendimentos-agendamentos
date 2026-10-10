from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import MembershipResponse
from app.auth.service import AuthService
from app.core.config import PasswordRecoveryConfigurationError, Settings
from app.models import (
    BillingCheckout,
    BusinessUserMembership,
    CommercialSubscription,
    ReengagementDelivery,
    User,
)
from app.models.billing import billing_provider_environment
from app.reengagement.email import BrevoReengagementMailer, ReengagementEmailError
from app.reengagement.schemas import ReengagementPromptResponse
from app.whatsapp.administration import (
    META_ONBOARDING_PENDING,
    WhatsAppConnectionAdministrationService,
)

logger = logging.getLogger(__name__)

Campaign = str
MAX_STEPS = 3
FIRST_DELAY = {
    "upgrade": timedelta(hours=24),
    "whatsapp_activation": timedelta(hours=2),
}
NEXT_DELAY = {
    "upgrade": {2: timedelta(days=3), 3: timedelta(days=5)},
    "whatsapp_activation": {2: timedelta(days=1), 3: timedelta(days=3)},
}


@dataclass(frozen=True, slots=True)
class CampaignMessage:
    subject: str
    title: str
    body: str
    cta_label: str
    cta_path: str
    footer: str


def campaign_message(
    campaign: Campaign,
    step: int,
    *,
    cta_path: str | None = None,
    action_hint: str | None = None,
) -> CampaignMessage:
    if campaign == "upgrade":
        messages = {
            1: CampaignMessage(
                subject="Seu cliente continua chamando enquanto a equipe está em campo",
                title="O cliente não sabe que você está ocupado",
                body=(
                    "Em uma empresa de refrigeração e climatização, a equipe pode estar em instalação, "
                    "manutenção ou deslocamento — mas o WhatsApp continua chegando. Quando cada conversa "
                    "depende de alguém parar para responder, o atendimento vira um gargalo. Com o plano "
                    "ativo, a Alovia organiza o fluxo de atendimento e agendamento da sua operação."
                ),
                cta_label="Ver planos e liberar a Alovia",
                cta_path="/app/mais/plano",
                footer="Você recebe este lembrete porque criou uma conta Alovia e ainda não liberou as funcionalidades operacionais.",
            ),
            2: CampaignMessage(
                subject="Seu WhatsApp pode deixar de ser só uma caixa de entrada",
                title="Uma conversa pode virar atendimento organizado",
                body=(
                    "Cliente chama, informa o que precisa, a demanda é organizada e o próximo passo pode "
                    "chegar à agenda. É essa sequência que tira peso do WhatsApp sem transformar sua rotina "
                    "em mais uma ferramenta complicada. Os planos pagos liberam o uso da Alovia com os dados "
                    "reais da sua empresa."
                ),
                cta_label="Quero organizar meu atendimento",
                cta_path="/app/mais/plano",
                footer="Você recebe este lembrete porque sua conta Alovia continua no modo sem funcionalidades operacionais liberadas.",
            ),
            3: CampaignMessage(
                subject="Seu atendimento não precisa parar quando começa a visita técnica",
                title="A operação pode continuar enquanto sua equipe trabalha",
                body=(
                    "O problema não é receber mensagens. É precisar escolher entre responder o cliente que "
                    "acabou de chamar e terminar o serviço que já está em andamento. Com a Alovia liberada, "
                    "o fluxo configurado de atendimento e agendamento pode continuar funcionando para sua "
                    "empresa de refrigeração enquanto a equipe cuida do campo."
                ),
                cta_label="Ativar atendimento e agendamento",
                cta_path="/app/mais/plano",
                footer="Este é o último lembrete desta sequência para liberar as funcionalidades operacionais da sua conta Alovia.",
            ),
        }
        return messages[step]

    if campaign != "whatsapp_activation":
        raise ValueError("Unsupported reengagement campaign")

    hint = action_hint or "Abra a Alovia para continuar exatamente do ponto em que parou."
    messages = {
        1: CampaignMessage(
            subject="Seu plano já está liberado — falta ligar a Alovia ao WhatsApp",
            title="A Alovia já está pronta. Falta conectar o canal.",
            body=(
                "Seu acesso operacional já foi liberado. Para a melhoria chegar ao canal onde seus clientes "
                f"realmente chamam, falta concluir a conexão do WhatsApp. {hint}"
            ),
            cta_label="Continuar conexão",
            cta_path=cta_path or "/app/whatsapp",
            footer="Este lembrete aparece porque sua conta tem funcionalidades liberadas, mas o WhatsApp ainda precisa de uma ação sua.",
        ),
        2: CampaignMessage(
            subject="A configuração está feita, mas o atendimento ainda não entrou no WhatsApp",
            title="Falta uma etapa para colocar o fluxo em operação",
            body=(
                "A estrutura da Alovia pode estar pronta, mas sem terminar a conexão o atendimento continua "
                f"fora do canal principal da sua empresa. {hint}"
            ),
            cta_label="Retomar do ponto certo",
            cta_path=cta_path or "/app/whatsapp",
            footer="A Alovia interrompe esta sequência automaticamente assim que não houver mais uma ação de conexão pendente.",
        ),
        3: CampaignMessage(
            subject="Feche a última etapa para usar a Alovia no atendimento",
            title="Você já avançou. Agora vale fechar o ciclo.",
            body=(
                "Plano liberado e operação configurada só viram melhoria no atendimento quando o WhatsApp "
                f"também entra no fluxo. {hint}"
            ),
            cta_label="Concluir ativação do WhatsApp",
            cta_path=cta_path or "/app/whatsapp",
            footer="Este é o último lembrete desta sequência de ativação do WhatsApp.",
        ),
    }
    return messages[step]


class ReengagementService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def claim_prompt(
        self,
        *,
        user_id: UUID,
        membership: MembershipResponse,
    ) -> ReengagementPromptResponse | None:
        if membership.role not in {"owner", "admin"}:
            return None
        user = await self.db.get(User, user_id)
        if user is None or not user.is_active:
            return None
        eligible = await self._eligible(membership)
        if eligible is None:
            return None
        campaign, cta_path, action_hint = eligible
        delivery = await self._ensure_due_delivery(
            user=user,
            business_id=membership.business_id,
            campaign=campaign,
            now=datetime.now(UTC),
        )
        if delivery is None or delivery.popup_shown_at is not None:
            return None
        delivery.popup_shown_at = datetime.now(UTC)
        message = campaign_message(
            campaign,
            delivery.step,
            cta_path=cta_path,
            action_hint=action_hint,
        )
        await self.db.commit()
        return ReengagementPromptResponse(
            id=delivery.id,
            campaign=campaign,  # type: ignore[arg-type]
            step=delivery.step,
            title=message.title,
            body=message.body,
            cta_label=message.cta_label,
            cta_path=message.cta_path,
        )

    async def record_interaction(
        self,
        *,
        delivery_id: UUID,
        user_id: UUID,
        business_id: UUID,
        action: str,
    ) -> None:
        delivery = await self.db.scalar(
            select(ReengagementDelivery).where(
                ReengagementDelivery.id == delivery_id,
                ReengagementDelivery.user_id == user_id,
                ReengagementDelivery.business_id == business_id,
            )
        )
        if delivery is None:
            return
        now = datetime.now(UTC)
        if action == "dismiss":
            delivery.popup_dismissed_at = delivery.popup_dismissed_at or now
        elif action == "cta":
            delivery.cta_clicked_at = delivery.cta_clicked_at or now
        else:
            raise ValueError("Unsupported interaction")
        await self.db.commit()

    async def send_due_emails(self, settings: Settings, *, limit: int = 100) -> int:
        try:
            config = settings.require_application_email_configuration()
        except PasswordRecoveryConfigurationError:
            logger.info("reengagement_email_disabled")
            return 0

        rows = (
            await self.db.execute(
                select(User, BusinessUserMembership)
                .join(
                    BusinessUserMembership,
                    BusinessUserMembership.user_id == User.id,
                )
                .where(
                    User.is_active.is_(True),
                    BusinessUserMembership.role.in_(("owner", "admin")),
                )
                .order_by(User.created_at, User.id, BusinessUserMembership.business_id)
                .limit(limit)
            )
        ).all()
        mailer = BrevoReengagementMailer(config)
        sent = 0
        for user, membership_row in rows:
            memberships = await AuthService(self.db).memberships(user)
            membership = next(
                (
                    item
                    for item in memberships
                    if item.business_id == membership_row.business_id
                ),
                None,
            )
            if membership is None:
                continue
            eligible = await self._eligible(membership)
            if eligible is None:
                continue
            campaign, cta_path, action_hint = eligible
            delivery = await self._ensure_due_delivery(
                user=user,
                business_id=membership.business_id,
                campaign=campaign,
                now=datetime.now(UTC),
            )
            if delivery is None or delivery.email_sent_at is not None:
                continue
            message = campaign_message(
                campaign,
                delivery.step,
                cta_path=cta_path,
                action_hint=action_hint,
            )
            try:
                await mailer.send(
                    delivery_id=delivery.id,
                    email=user.email,
                    subject=message.subject,
                    title=message.title,
                    body=message.body,
                    cta_label=message.cta_label,
                    cta_path=message.cta_path,
                    footer=message.footer,
                )
            except ReengagementEmailError:
                await self.db.rollback()
                logger.warning(
                    "reengagement_email_failed",
                    extra={"campaign": campaign, "step": delivery.step},
                )
                continue
            delivery.email_sent_at = datetime.now(UTC)
            await self.db.commit()
            sent += 1
        return sent

    async def _eligible(
        self,
        membership: MembershipResponse,
    ) -> tuple[str, str, str] | None:
        if membership.access_mode == "free":
            return (
                "upgrade",
                "/app/mais/plano",
                "Veja os planos e escolha quando fizer sentido liberar a operação.",
            )
        if membership.access_mode != "paid":
            return None

        connection = await WhatsAppConnectionAdministrationService(
            self.db
        ).get_connection(membership.business_id)
        if connection is None or connection.status.value == "disconnected":
            return (
                "whatsapp_activation",
                "/app/whatsapp?continuar=1",
                "Comece pela escolha de como este número será usado; a Alovia conduz as etapas seguintes.",
            )
        if connection.status.value == "connected":
            return None
        if (
            connection.status.value == "pending"
            and connection.mode.value == "coexistence"
            and connection.has_phone_number_id
            and connection.meta_review_status is None
            and connection.last_error_code == META_ONBOARDING_PENDING
        ):
            # The customer already did their part. Never nag them to reconnect
            # while Meta is reviewing the account.
            return None
        if (
            connection.status.value == "pending"
            and connection.meta_review_status == "rejected"
        ):
            return (
                "whatsapp_activation",
                "/app/whatsapp",
                "A Meta encerrou a análise sem aprovação. Não reconecte no escuro: abra a orientação, revise a pendência e só então tente novamente.",
            )
        if connection.status.value == "pending" and connection.mode.value == "coexistence":
            return (
                "whatsapp_activation",
                "/app/whatsapp/business?auto=1",
                "A conexão já começou. Falta concluir a autorização oficial da Meta.",
            )
        if connection.status.value == "pending" and connection.mode.value == "api_only":
            return (
                "whatsapp_activation",
                "/app/whatsapp/exclusivo",
                "A conexão já começou. Continue a ativação exclusiva pela Alovia.",
            )
        return (
            "whatsapp_activation",
            "/app/whatsapp?continuar=1",
            "A conexão precisa de atenção. A Alovia vai levar você ao ponto correto para resolver sem repetir etapas desnecessárias.",
        )

    async def _ensure_due_delivery(
        self,
        *,
        user: User,
        business_id: UUID,
        campaign: str,
        now: datetime,
    ) -> ReengagementDelivery | None:
        deliveries = list(
            (
                await self.db.scalars(
                    select(ReengagementDelivery)
                    .where(
                        ReengagementDelivery.user_id == user.id,
                        ReengagementDelivery.business_id == business_id,
                        ReengagementDelivery.campaign == campaign,
                    )
                    .order_by(ReengagementDelivery.step)
                )
            ).all()
        )
        if deliveries:
            last = deliveries[-1]
            if last.step >= MAX_STEPS:
                return last
            next_step = last.step + 1
            due_at = last.created_at + NEXT_DELAY[campaign][next_step]
            if now < due_at:
                return last
        else:
            next_step = 1
            anchor = (
                await self._whatsapp_activation_anchor(
                    business_id=business_id,
                    fallback=user.created_at,
                    now=now,
                )
                if campaign == "whatsapp_activation"
                else user.created_at
            )
            due_at = anchor + FIRST_DELAY[campaign]
            if now < due_at:
                return None

        existing = next(
            (delivery for delivery in deliveries if delivery.step == next_step),
            None,
        )
        if existing is not None:
            return existing
        delivery = ReengagementDelivery(
            id=uuid4(),
            user_id=user.id,
            business_id=business_id,
            campaign=campaign,
            step=next_step,
        )
        self.db.add(delivery)
        await self.db.flush()
        return delivery

    async def _whatsapp_activation_anchor(
        self,
        *,
        business_id: UUID,
        fallback: datetime,
        now: datetime,
    ) -> datetime:
        paid_at = await self.db.scalar(
            select(func.max(BillingCheckout.paid_at))
            .join(
                CommercialSubscription,
                CommercialSubscription.checkout_id == BillingCheckout.id,
            )
            .where(
                CommercialSubscription.business_id == business_id,
                CommercialSubscription.provider_environment
                == billing_provider_environment(),
                CommercialSubscription.status.in_(("active", "canceled")),
                CommercialSubscription.access_until > now,
                BillingCheckout.paid_at.is_not(None),
            )
        )
        return paid_at or fallback
