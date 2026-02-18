"""
WAF INTEGRATION
==============
Placeholder for Web Application Firewall configuration guidance.

WHY:
- WAF blocks common web exploits (SQLi/XSS/bots) before they reach the app.

HOW:
- Deploy behind a WAF (e.g., Cloudflare/Azure WAF) and route traffic through it.
- Optionally validate WAF headers or tokens here if your provider supports it.
"""

from __future__ import annotations

from Security.security_config import feature_enabled


def validate_waf_headers(headers: dict) -> bool:
    """Optional hook to validate WAF headers (provider-specific)."""
    if not feature_enabled("waf", True):
        return True
    return True
