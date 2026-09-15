"""Platform SMTP service for password resets, invites, and notifications."""

import html
import logging
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from storage import database as db
from storage.identity.credential_store import _encrypt, _decrypt

logger = logging.getLogger("claude-proxy")


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    from_addr: str
    use_tls: bool


def load_smtp_config() -> SmtpConfig | None:
    """Load SMTP configuration from platform_settings. Returns None if not configured."""
    settings = db.get_all_platform_settings()
    host = settings.get("smtp_host", "")
    if not host:
        return None
    port_str = settings.get("smtp_port", "587")
    try:
        port = int(port_str)
    except ValueError:
        port = 587
    user = settings.get("smtp_user", "")
    password_enc = settings.get("smtp_password_enc", "")
    password = ""
    if password_enc:
        try:
            password = _decrypt(password_enc)
        except Exception:
            logger.error("Failed to decrypt SMTP password")
            return None
    from_addr = settings.get("smtp_from", user)
    use_tls = settings.get("smtp_tls", "true").lower() == "true"
    return SmtpConfig(host=host, port=port, user=user, password=password,
                      from_addr=from_addr, use_tls=use_tls)


def save_smtp_config(host: str, port: str, user: str, password: str | None,
                     from_addr: str, use_tls: str) -> None:
    """Save SMTP configuration to platform_settings."""
    db.set_platform_setting("smtp_host", host)
    db.set_platform_setting("smtp_port", port)
    db.set_platform_setting("smtp_user", user)
    db.set_platform_setting("smtp_from", from_addr)
    db.set_platform_setting("smtp_tls", use_tls)
    if password is not None and password != "":
        db.set_platform_setting("smtp_password_enc", _encrypt(password))


def is_smtp_configured() -> bool:
    """Check if SMTP is configured (host is set)."""
    return bool(db.get_platform_setting("smtp_host"))


def send_email(to: str, subject: str, html_body: str,
               smtp_config: SmtpConfig | None = None) -> bool:
    """Send an email via SMTP. Returns True on success."""
    cfg = smtp_config or load_smtp_config()
    if not cfg:
        logger.warning("SMTP not configured, cannot send email")
        return False

    # Strip CR/LF so a crafted subject/recipient can't inject extra MIME headers.
    subject = subject.replace("\r", " ").replace("\n", " ")
    to = to.replace("\r", "").replace("\n", "")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html"))

    try:
        if cfg.use_tls:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=10)
            server.starttls()
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=10)

        if cfg.user and cfg.password:
            server.login(cfg.user, cfg.password)
        server.sendmail(cfg.from_addr, [to], msg.as_string())
        server.quit()
        logger.info("Email sent")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


def test_smtp_connection(host: str, port: int, user: str, password: str,
                         use_tls: bool, test_email: str = "") -> tuple[bool, str]:
    """Test SMTP connection. Optionally send a test email."""
    try:
        if use_tls:
            server = smtplib.SMTP(host, port, timeout=10)
            server.starttls()
        else:
            server = smtplib.SMTP(host, port, timeout=10)

        if user and password:
            server.login(user, password)

        if test_email:
            msg = MIMEText("This is a test email from OtoDock platform.", "plain")
            msg["Subject"] = "OtoDock — SMTP Test"
            msg["From"] = user
            msg["To"] = test_email
            server.sendmail(user, [test_email], msg.as_string())
            server.quit()
            return True, f"Test email sent to {test_email}"

        server.noop()
        server.quit()
        return True, "Connection successful"
    except smtplib.SMTPAuthenticationError:
        return False, "Authentication failed — check username and password"
    except smtplib.SMTPConnectError:
        return False, f"Could not connect to {host}:{port}"
    except Exception as e:
        return False, f"Connection failed: {str(e)}"


# --- Email templates ---


def send_password_reset_email(to: str, reset_url: str) -> bool:
    """Send a password reset email."""
    body = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
        <h2>Password Reset</h2>
        <p>You requested a password reset for your OtoDock account.</p>
        <p><a href="{reset_url}" style="display: inline-block; padding: 12px 24px;
            background-color: #3B82F6; color: white; text-decoration: none;
            border-radius: 8px;">Reset Password</a></p>
        <p style="color: #666; font-size: 14px;">This link expires in 1 hour.
        If you didn't request this, you can safely ignore this email.</p>
    </div>
    """
    return send_email(to, "OtoDock — Password Reset", body)


def send_invite_email(to: str, invite_url: str, inviter_name: str = "") -> bool:
    """Send a new user invite email (tokenized accept-invite link)."""
    invited_by = f" by {html.escape(inviter_name)}" if inviter_name else ""
    body = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
        <h2>You've been invited to OtoDock</h2>
        <p>You've been invited{invited_by} to join the OtoDock AI Agents platform.
        Click below to choose your password and activate your account.</p>
        <p><a href="{invite_url}" style="display: inline-block; padding: 12px 24px;
            background-color: #3B82F6; color: white; text-decoration: none;
            border-radius: 8px;">Set Up Your Account</a></p>
        <p style="color: #666; font-size: 14px;">This invitation link expires in 48 hours.
        If it has expired, ask your administrator for a new one.</p>
    </div>
    """
    return send_email(to, "OtoDock — You're Invited", body)
