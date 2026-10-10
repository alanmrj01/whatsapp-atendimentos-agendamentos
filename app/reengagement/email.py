from __future__ import annotations

from html import escape
from uuid import UUID

import httpx

from app.core.config import PasswordResetEmailConfiguration


class ReengagementEmailError(RuntimeError):
    """Safe provider failure without response-body leakage."""


class BrevoReengagementMailer:
    def __init__(self, configuration: PasswordResetEmailConfiguration) -> None:
        self.configuration = configuration

    async def send(
        self,
        *,
        delivery_id: UUID,
        email: str,
        subject: str,
        title: str,
        body: str,
        cta_label: str,
        cta_path: str,
        footer: str,
    ) -> None:
        cta_url = f"{self.configuration.public_base_url}{cta_path}"
        logo_url = f"{self.configuration.public_base_url}/app-icon-192.png"
        text = (
            f"{title}\n\n{body}\n\n"
            f"{cta_label}: {cta_url}\n\n"
            f"{footer}\n\nEquipe Alovia"
        )
        html = f"""<!doctype html>
<html lang="pt-BR">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(subject)}</title>
  </head>
  <body style="margin:0;padding:0;background:#f4f7fb;font-family:Arial,Helvetica,sans-serif;color:#12263a;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;background:#f4f7fb;">
      <tr><td align="center" style="padding:30px 16px;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:560px;background:#fff;border:1px solid #e2e8f0;border-radius:20px;">
          <tr><td style="padding:30px;">
            <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin-bottom:24px;">
              <tr>
                <td style="padding-right:10px;"><img src="{escape(logo_url, quote=True)}" width="42" height="42" alt="Alovia" style="display:block;border:0;"></td>
                <td style="font-size:23px;font-weight:700;color:#102a43;">Alovia</td>
              </tr>
            </table>
            <h1 style="margin:0 0 14px;font-size:27px;line-height:35px;color:#102a43;">{escape(title)}</h1>
            <p style="margin:0 0 24px;font-size:16px;line-height:25px;color:#52667a;">{escape(body)}</p>
            <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:0 0 24px;">
              <tr><td bgcolor="#0b67f0" style="border-radius:12px;">
                <a href="{escape(cta_url, quote=True)}" style="display:inline-block;padding:14px 22px;font-size:16px;font-weight:700;color:#fff;text-decoration:none;border-radius:12px;">{escape(cta_label)}</a>
              </td></tr>
            </table>
            <div style="height:1px;background:#e7edf3;margin:0 0 20px;"></div>
            <p style="margin:0;font-size:12px;line-height:19px;color:#7b8794;">{escape(footer)}</p>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""
        payload = {
            "sender": {
                "name": self.configuration.from_name,
                "email": self.configuration.from_email,
            },
            "to": [{"email": email}],
            "subject": subject,
            "textContent": text,
            "htmlContent": html,
            # Brevo requires a UUID idempotency key. Reusing the delivery UUID
            # makes retries within the provider TTL safe as well.
            "headers": {"idempotencyKey": str(delivery_id)},
        }
        headers = {
            "Accept": "application/json",
            "api-key": self.configuration.api_key.get_secret_value(),
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(
                    "https://api.brevo.com/v3/smtp/email",
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise ReengagementEmailError("Reengagement email delivery failed") from exc
        if not 200 <= response.status_code < 300:
            raise ReengagementEmailError("Reengagement email delivery failed")
