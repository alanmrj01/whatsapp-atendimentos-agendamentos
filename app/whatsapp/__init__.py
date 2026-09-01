"""Integrações isoladas com o WhatsApp Cloud API."""

from app.whatsapp.client import (
    WhatsAppAuthenticationError,
    WhatsAppClient,
    WhatsAppClientError,
    WhatsAppInvalidResponseError,
    WhatsAppNetworkError,
    WhatsAppPermanentError,
    WhatsAppRateLimitError,
    WhatsAppTemporaryError,
    WhatsAppTimeoutError,
    WhatsAppValidationError,
)

__all__ = [
    "WhatsAppAuthenticationError",
    "WhatsAppClient",
    "WhatsAppClientError",
    "WhatsAppInvalidResponseError",
    "WhatsAppNetworkError",
    "WhatsAppPermanentError",
    "WhatsAppRateLimitError",
    "WhatsAppTemporaryError",
    "WhatsAppTimeoutError",
    "WhatsAppValidationError",
]
