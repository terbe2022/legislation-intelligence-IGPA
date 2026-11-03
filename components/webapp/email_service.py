"""
email_service.py — Azure Communication Services email sender
=============================================================
Sends magic-link emails for authentication and (later) weekly digests.

Environment variables required:
  ACS_CONNECTION_STRING   from Azure Communication Services resource → Keys
  ACS_SENDER_ADDRESS      verified sender, e.g. noreply@yourdomain.com
                          (must be a verified domain in your ACS resource)

If either env var is missing, send_magic_link() logs a warning and returns
the link as a string so developers can test locally without ACS configured.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTML email template
# ---------------------------------------------------------------------------

def _build_magic_link_html(link: str, email: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to IL Legislative Intelligence</title>
</head>
<body style="margin:0;padding:0;background:#F8FAFC;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F8FAFC;padding:40px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;border:1px solid #E2E8F0;overflow:hidden;">

        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#0F1F38 0%,#1B4B5A 55%,#2D6A7A 100%);padding:32px 40px;">
          <div style="font-size:28px;margin-bottom:6px;">🏛</div>
          <div style="color:#fff;font-size:20px;font-weight:800;letter-spacing:-.03em;">
            IL Legislative Intelligence
          </div>
          <div style="color:rgba(255,255,255,.65);font-size:12px;margin-top:4px;">
            Institute of Government and Public Affairs · University of Illinois System
          </div>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:36px 40px;">
          <p style="margin:0 0 8px;font-size:15px;color:#0F172A;font-weight:600;">
            Sign in to your account
          </p>
          <p style="margin:0 0 28px;font-size:14px;color:#454F5A;line-height:1.6;">
            We received a sign-in request for <strong>{email}</strong>.
            Click the button below to sign in. This link expires in <strong>1 hour</strong>
            and can only be used once.
          </p>

          <!-- CTA button -->
          <table cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
            <tr><td style="background:#13294B;border-radius:10px;">
              <a href="{link}"
                 style="display:inline-block;padding:14px 32px;color:#fff;font-size:15px;
                        font-weight:700;text-decoration:none;letter-spacing:-.01em;">
                Sign In to Platform →
              </a>
            </td></tr>
          </table>

          <!-- Fallback link -->
          <p style="margin:0 0 6px;font-size:12px;color:#454F5A;">
            If the button doesn't work, copy and paste this URL:
          </p>
          <p style="margin:0 0 28px;font-size:11px;color:#454F5A;word-break:break-all;">
            <a href="{link}" style="color:#13294B;">{link}</a>
          </p>

          <!-- Security note -->
          <div style="background:#FFF8E1;border:1px solid #FDE68A;border-radius:8px;padding:14px 16px;">
            <p style="margin:0;font-size:12px;color:#92400E;line-height:1.6;">
              <strong>Didn't request this?</strong> You can safely ignore this email.
              Someone may have entered your address by mistake. Your account is secure —
              no one can sign in without clicking this link from your inbox.
            </p>
          </div>
        </td></tr>

        <!-- Footer -->
        <tr><td style="padding:20px 40px;border-top:1px solid #F1F5F9;">
          <p style="margin:0;font-size:11px;color:#454F5A;line-height:1.6;">
            University of Illinois System · Institute of Government and Public Affairs<br>
            This email was sent because a sign-in was requested for {email}.<br>
            For support contact your IGPA platform administrator.
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _build_magic_link_text(link: str, email: str) -> str:
    return f"""IL Legislative Intelligence — Sign In

A sign-in was requested for {email}.

Click the link below to sign in (expires in 1 hour, single use):

{link}

If you didn't request this, ignore this email. Your account is secure.

---
University of Illinois System · Institute of Government and Public Affairs
"""


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------

def send_magic_link(email: str, link: str) -> tuple[bool, str]:
    """
    Send a magic-link email via Azure Communication Services.

    Returns (success: bool, message: str).
    If ACS is not configured, returns (False, link) so callers can surface
    the link for local dev.
    """
    connection_string = os.environ.get("ACS_CONNECTION_STRING", "")
    sender_address    = os.environ.get("ACS_SENDER_ADDRESS", "")

    if not connection_string or not sender_address:
        logger.warning(
            "ACS not configured — magic link NOT emailed. Link: %s", link
        )
        return False, link  # Caller can show link in dev mode

    try:
        from azure.communication.email import EmailClient  # type: ignore

        client = EmailClient.from_connection_string(connection_string)

        message = {
            "senderAddress": sender_address,
            "recipients": {
                "to": [{"address": email}],
            },
            "content": {
                "subject": "Your sign-in link for IL Legislative Intelligence",
                "plainText": _build_magic_link_text(link, email),
                "html":      _build_magic_link_html(link, email),
            },
        }

        poller = client.begin_send(message)
        result = poller.result()   # blocks until sent / failed
        logger.info("Magic link email sent to %s, result: %s", email, result)
        return True, "Email sent."

    except ImportError:
        logger.error("azure-communication-email not installed")
        return False, link
    except Exception as e:
        logger.error("ACS send failed for %s: %s", email, e)
        return False, str(e)


# ---------------------------------------------------------------------------
# Team invite email
# ---------------------------------------------------------------------------

def _build_team_invite_html(team_name: str, inviter_name: str, app_url: str) -> str:
    _cta = (f"""
          <table cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
            <tr><td style="background:#13294B;border-radius:10px;">
              <a href="{app_url}"
                 style="display:inline-block;padding:14px 32px;color:#fff;font-size:15px;
                        font-weight:700;text-decoration:none;letter-spacing:-.01em;">
                Sign In to Accept →
              </a>
            </td></tr>
          </table>
          <p style="margin:0 0 6px;font-size:12px;color:#454F5A;">
            If the button doesn't work, copy and paste this URL:
          </p>
          <p style="margin:0 0 28px;font-size:11px;color:#454F5A;word-break:break-all;">
            <a href="{app_url}" style="color:#13294B;">{app_url}</a>
          </p>""" if app_url else "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>You've been invited to a team</title>
</head>
<body style="margin:0;padding:0;background:#F8FAFC;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F8FAFC;padding:40px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;border:1px solid #E2E8F0;overflow:hidden;">

        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#0F1F38 0%,#1B4B5A 55%,#2D6A7A 100%);padding:32px 40px;">
          <div style="font-size:28px;margin-bottom:6px;">🏛</div>
          <div style="color:#fff;font-size:20px;font-weight:800;letter-spacing:-.03em;">
            IL Legislative Intelligence
          </div>
          <div style="color:rgba(255,255,255,.65);font-size:12px;margin-top:4px;">
            Institute of Government and Public Affairs · University of Illinois System
          </div>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:36px 40px;">
          <p style="margin:0 0 8px;font-size:15px;color:#0F172A;font-weight:600;">
            You've been invited to a team
          </p>
          <p style="margin:0 0 28px;font-size:14px;color:#454F5A;line-height:1.6;">
            <strong>{inviter_name}</strong> invited you to join the team
            <strong>{team_name}</strong> on the IL Legislative Intelligence platform.
            Sign in with your University of Illinois email, then go to
            <strong>My Profile → Team</strong> and click <strong>Accept</strong>.
          </p>
          {_cta}
          <!-- note -->
          <div style="background:#FFF8E1;border:1px solid #FDE68A;border-radius:8px;padding:14px 16px;">
            <p style="margin:0;font-size:12px;color:#92400E;line-height:1.6;">
              <strong>Didn't expect this?</strong> You can ignore this email — nothing
              happens to your account until you sign in and accept the invite yourself.
            </p>
          </div>
        </td></tr>

        <!-- Footer -->
        <tr><td style="padding:20px 40px;border-top:1px solid #F1F5F9;">
          <p style="margin:0;font-size:11px;color:#454F5A;line-height:1.6;">
            University of Illinois System · Institute of Government and Public Affairs<br>
            Only University of Illinois System accounts can join teams (@illinois.edu, @uillinois.edu, @uic.edu, @uis.edu).<br>
            For support contact your IGPA platform administrator.
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _build_team_invite_text(team_name: str, inviter_name: str, app_url: str) -> str:
    _link = f"\nSign in to accept:\n{app_url}\n" if app_url else ""
    return f"""IL Legislative Intelligence — Team Invite

{inviter_name} invited you to join the team "{team_name}".

Sign in with your University of Illinois email, then go to
My Profile -> Team and click Accept.
{_link}
Didn't expect this? You can ignore this email — nothing happens to your
account until you sign in and accept the invite yourself.

---
University of Illinois System · Institute of Government and Public Affairs
Only University of Illinois System accounts can join teams (@illinois.edu, @uillinois.edu, @uic.edu, @uis.edu).
"""


def send_team_invite(email: str, team_name: str,
                     inviter_name: str) -> tuple[bool, str]:
    """
    Notify an invitee that they've been added to a team. Best-effort: mirrors
    send_magic_link. Returns (success, message). The link points at the app's
    base URL (MAGIC_LINK_BASE_URL) — the recipient signs in normally and accepts
    from My Profile, so no expiring token is embedded.
    """
    connection_string = os.environ.get("ACS_CONNECTION_STRING", "")
    sender_address    = os.environ.get("ACS_SENDER_ADDRESS", "")
    app_url           = os.environ.get("MAGIC_LINK_BASE_URL", "").strip()

    if not connection_string or not sender_address:
        logger.warning("ACS not configured — team invite NOT emailed to %s", email)
        return False, "ACS not configured"

    try:
        from azure.communication.email import EmailClient  # type: ignore

        client = EmailClient.from_connection_string(connection_string)
        message = {
            "senderAddress": sender_address,
            "recipients": {"to": [{"address": email}]},
            "content": {
                "subject": f"You've been invited to {team_name} · IL Legislative Intelligence",
                "plainText": _build_team_invite_text(team_name, inviter_name, app_url),
                "html":      _build_team_invite_html(team_name, inviter_name, app_url),
            },
        }
        poller = client.begin_send(message)
        poller.result()
        logger.info("Team invite email sent to %s for team %s", email, team_name)
        return True, "Email sent."
    except ImportError:
        logger.error("azure-communication-email not installed")
        return False, "email SDK missing"
    except Exception as e:
        logger.error("Team invite send failed for %s: %s", email, e)
        return False, str(e)


# ---------------------------------------------------------------------------
# Weekly digest stub (to be fleshed out in pipeline)
# ---------------------------------------------------------------------------

def send_digest(email: str, subject: str, html_body: str) -> bool:
    """Send a digest email. Returns True on success."""
    connection_string = os.environ.get("ACS_CONNECTION_STRING", "")
    sender_address    = os.environ.get("ACS_SENDER_ADDRESS", "")

    if not connection_string or not sender_address:
        logger.warning("ACS not configured — digest not sent for %s", email)
        return False

    try:
        from azure.communication.email import EmailClient  # type: ignore

        client  = EmailClient.from_connection_string(connection_string)
        message = {
            "senderAddress": sender_address,
            "recipients": {"to": [{"address": email}]},
            "content": {
                "subject":   subject,
                "html":      html_body,
                "plainText": "Please view this email in an HTML-capable client.",
            },
        }
        poller = client.begin_send(message)
        poller.result()
        return True
    except Exception as e:
        logger.error("Digest send failed for %s: %s", email, e)
        return False
