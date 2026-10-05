from __future__ import annotations

from html import escape
from urllib.parse import urlencode
from uuid import UUID

import httpx

from app.core.config import PasswordResetEmailConfiguration


class PasswordResetEmailError(RuntimeError):
    """Safe delivery error that never includes provider response bodies."""


class BrevoPasswordResetMailer:
    def __init__(self, configuration: PasswordResetEmailConfiguration) -> None:
        self.configuration = configuration

    async def send(self, *, reset_id: UUID, email: str, token: str) -> None:
        reset_url = (
            f"{self.configuration.public_base_url}/redefinir-senha#"
            + urlencode({"token": token})
        )
        subject = "Redefina sua senha da Alovia"
        text = (
            "Recebemos uma solicitação para redefinir sua senha da Alovia.\n\n"
            f"Use este link nos próximos 30 minutos: {reset_url}\n\n"
            "O link pode ser usado uma única vez.\n\n"
            "Se você não solicitou a alteração, ignore esta mensagem."
        )
        html = (
            "<p>Recebemos uma solicitação para redefinir sua senha da Alovia.</p>"
            "<p>O link abaixo é válido por 30 minutos e pode ser usado uma única vez.</p>"
            f'<p><a href="{escape(reset_url, quote=True)}">Redefinir minha senha</a></p>'
            "<p>Se você não solicitou a alteração, ignore esta mensagem.</p>"
        )
        headers = {
            "Accept": "application/json",
            "api-key": self.configuration.api_key.get_secret_value(),
            "Content-Type": "application/json",
        }
        payload = {
            "sender": {
                "name": self.configuration.from_name,
                "email": self.configuration.from_email,
            },
            "to": [{"email": email}],
            "subject": subject,
            "textContent": text,
            "htmlContent": html,
            "headers": {"idempotencyKey": str(reset_id)},
        }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(
                    "https://api.brevo.com/v3/smtp/email",
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            # Do not retry an ambiguous transport failure automatically.
            # A user can request a new reset link; the previous unused token
            # will then be revoked by the issuance flow.
            raise PasswordResetEmailError(
                "Password reset email delivery failed"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise PasswordResetEmailError(
                "Password reset email delivery failed"
            )
