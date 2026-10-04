from __future__ import annotations

from html import escape
from urllib.parse import urlencode

import httpx

from app.core.config import PasswordResetEmailConfiguration


class PasswordResetEmailError(RuntimeError):
    """Safe delivery error that never includes provider response bodies."""


class ResendPasswordResetMailer:
    def __init__(self, configuration: PasswordResetEmailConfiguration) -> None:
        self.configuration = configuration

    async def send(self, *, email: str, token: str) -> None:
        reset_url = (
            f"{self.configuration.public_base_url}/redefinir-senha?"
            + urlencode({"token": token})
        )
        subject = "Redefina sua senha da Alovia"
        text = (
            "Recebemos uma solicitação para redefinir sua senha da Alovia.\n\n"
            f"Use este link nos próximos 30 minutos: {reset_url}\n\n"
            "Se você não solicitou a alteração, ignore esta mensagem."
        )
        html = (
            "<p>Recebemos uma solicitação para redefinir sua senha da Alovia.</p>"
            "<p>O link abaixo é válido por 30 minutos e pode ser usado uma única vez.</p>"
            f'<p><a href="{escape(reset_url, quote=True)}">Redefinir minha senha</a></p>'
            "<p>Se você não solicitou a alteração, ignore esta mensagem.</p>"
        )
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": (
                            "Bearer "
                            + self.configuration.api_key.get_secret_value()
                        ),
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": self.configuration.from_email,
                        "to": [email],
                        "subject": subject,
                        "text": text,
                        "html": html,
                    },
                )
        except httpx.HTTPError as exc:
            raise PasswordResetEmailError(
                "Password reset email delivery failed"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise PasswordResetEmailError(
                "Password reset email delivery failed"
            )
