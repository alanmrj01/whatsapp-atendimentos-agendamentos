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
        logo_url = f"{self.configuration.public_base_url}/app-icon-192.png"
        escaped_reset_url = escape(reset_url, quote=True)
        escaped_logo_url = escape(logo_url, quote=True)
        text = (
            "Redefina sua senha da Alovia\n\n"
            "Recebemos uma solicitação para criar uma nova senha para sua conta.\n\n"
            f"Redefina sua senha aqui: {reset_url}\n\n"
            "Por segurança, este link é válido por 30 minutos e pode ser usado "
            "uma única vez.\n\n"
            "Se você não solicitou essa alteração, ignore este e-mail.\n\n"
            "Equipe Alovia"
        )
        html = f"""<!doctype html>
<html lang="pt-BR">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Redefina sua senha da Alovia</title>
  </head>
  <body style="margin:0;padding:0;background:#f4f7fb;font-family:Arial,Helvetica,sans-serif;color:#12263a;">
    <div style="display:none;max-height:0;overflow:hidden;opacity:0;">
      Use este link nos próximos 30 minutos para redefinir sua senha da Alovia.
    </div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;background:#f4f7fb;">
      <tr>
        <td align="center" style="padding:32px 16px;">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:560px;background:#ffffff;border:1px solid #e2e8f0;border-radius:20px;">
            <tr>
              <td style="padding:32px 32px 28px;">
                <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin-bottom:28px;">
                  <tr>
                    <td style="vertical-align:middle;padding-right:10px;">
                      <img src="{escaped_logo_url}" width="44" height="44" alt="Alovia" style="display:block;width:44px;height:44px;border:0;">
                    </td>
                    <td style="vertical-align:middle;font-size:24px;line-height:28px;font-weight:700;color:#102a43;">
                      Alovia
                    </td>
                  </tr>
                </table>

                <div style="margin-bottom:10px;font-size:12px;line-height:18px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#0b67f0;">
                  Recuperação de acesso
                </div>

                <h1 style="margin:0 0 14px;font-size:30px;line-height:38px;font-weight:700;color:#102a43;">
                  Redefina sua senha
                </h1>

                <p style="margin:0 0 24px;font-size:16px;line-height:25px;color:#52667a;">
                  Recebemos uma solicitação para criar uma nova senha para sua conta Alovia.
                </p>

                <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:0 0 24px;">
                  <tr>
                    <td bgcolor="#0b67f0" style="border-radius:12px;">
                      <a href="{escaped_reset_url}" style="display:inline-block;padding:14px 22px;font-size:16px;line-height:20px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:12px;">
                        Redefinir minha senha
                      </a>
                    </td>
                  </tr>
                </table>

                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;margin:0 0 24px;background:#eef8f3;border:1px solid #cde8da;border-radius:12px;">
                  <tr>
                    <td style="padding:14px 16px;font-size:14px;line-height:22px;color:#246b4a;">
                      Por segurança, este link é válido por <strong>30 minutos</strong> e pode ser usado <strong>uma única vez</strong>.
                    </td>
                  </tr>
                </table>

                <p style="margin:0 0 8px;font-size:13px;line-height:20px;color:#718096;">
                  Se o botão não funcionar, copie e cole este endereço no navegador:
                </p>
                <p style="margin:0 0 26px;font-size:12px;line-height:19px;word-break:break-all;">
                  <a href="{escaped_reset_url}" style="color:#0b67f0;text-decoration:underline;">{escape(reset_url)}</a>
                </p>

                <div style="height:1px;background:#e7edf3;margin:0 0 22px;"></div>

                <p style="margin:0 0 6px;font-size:13px;line-height:21px;color:#718096;">
                  Se você não solicitou essa alteração, ignore este e-mail. Sua senha atual continuará a mesma.
                </p>
                <p style="margin:0;font-size:13px;line-height:21px;color:#718096;">
                  Equipe Alovia
                </p>
              </td>
            </tr>
          </table>

          <p style="margin:18px 0 0;font-size:12px;line-height:18px;color:#94a3b8;">
            Esta é uma mensagem automática de segurança da Alovia.
          </p>
        </td>
      </tr>
    </table>
  </body>
</html>"""
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
