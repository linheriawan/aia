from __future__ import annotations


class ProviderError(Exception):
    """Base for provider-level failures."""


class ProviderAuthError(ProviderError):
    """Credentials are missing, malformed, or rejected."""


class ProviderConnectionError(ProviderError):
    """The provider could not be reached, or returned an unexpected error."""
