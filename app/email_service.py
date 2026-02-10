import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable, Optional
import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .database import SessionLocal
from .models import EmailSettings, User

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR.parent / "templates"
LOGO_PATH = BASE_DIR / "static" / "assets" / "logo.png"

_jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"])
)


def _get_smtp_config() -> dict:
    smtp_user = ""
    smtp_pass = ""
    smtp_from = ""
    smtp_host = "smtp.gmail.com"
    smtp_port = "465"

    try:
        db = SessionLocal()
        settings = db.query(EmailSettings).order_by(EmailSettings.id.desc()).first()
        if settings:
            smtp_user = (settings.smtp_user or "").strip()
            smtp_pass = (settings.smtp_pass or "").strip()
            smtp_from = (settings.smtp_from or "").strip()
            smtp_host = (settings.smtp_host or smtp_host).strip()
            smtp_port = (settings.smtp_port or smtp_port).strip()
    except Exception:
        settings = None
    finally:
        try:
            db.close()
        except Exception:
            pass

    if not smtp_user:
        smtp_user = os.getenv("SMTP_USER", "").strip()
    if not smtp_pass:
        smtp_pass = os.getenv("SMTP_PASS", "").replace(" ", "").strip()
    if not smtp_from:
        smtp_from = os.getenv("SMTP_FROM", "").strip() or smtp_user
    if smtp_user and smtp_from and "@" not in smtp_from:
        smtp_from = f"{smtp_from} <{smtp_user}>"

    return {
        "host": smtp_host,
        "port": int(smtp_port or "465"),
        "user": smtp_user,
        "pass": smtp_pass,
        "from": smtp_from
    }


def _smtp_enabled() -> bool:
    config = _get_smtp_config()
    return bool(config["user"] and config["pass"] and config["from"])


def smtp_enabled() -> bool:
    return _smtp_enabled()


def _render_template(template_name: str, context: dict) -> str:
    template = _jinja_env.get_template(template_name)
    return template.render(**context)


def _get_company_context() -> dict:
    company_name = os.getenv("COMPANY_NAME", "TeamSync")
    year = os.getenv("COMPANY_YEAR", str(datetime.datetime.now().year))
    logo_cid = "teamsync_logo"
    logo_url = f"cid:{logo_cid}" if LOGO_PATH.exists() else os.getenv("COMPANY_LOGO_URL", "")
    return {
        "company_name": company_name,
        "year": year,
        "logo_url": logo_url,
        "logo_cid": logo_cid
    }


def send_email(to_email: str, subject: str, body: str, html_body: Optional[str] = None,
               inline_images: Optional[list[dict]] = None) -> bool:
    config = _get_smtp_config()
    if not _smtp_enabled() or not to_email:
        return False

    msg = EmailMessage()
    msg["From"] = config["from"]
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        html_part = msg.add_alternative(html_body, subtype="html")
        if LOGO_PATH.exists():
            try:
                html_part.add_related(
                    LOGO_PATH.read_bytes(),
                    maintype="image",
                    subtype="png",
                    cid=_get_company_context()["logo_cid"]
                )
            except Exception:
                pass
        if inline_images:
            for image in inline_images:
                try:
                    html_part.add_related(
                        image["data"],
                        maintype=image.get("maintype", "image"),
                        subtype=image.get("subtype", "jpeg"),
                        cid=image["cid"]
                    )
                except Exception:
                    continue

    try:
        with smtplib.SMTP_SSL(config["host"], config["port"]) as server:
            server.login(config["user"], config["pass"])
            server.send_message(msg)
        return True
    except Exception as exc:
        print(f"SMTP send failed: {exc}")
        return False


def _get_employee_context(employee_id: Optional[str], name: str) -> tuple[dict, list[dict]]:
    employee_initial = (name or "?")[:1].upper()
    photo_cid = "employee_photo"
    inline_images: list[dict] = []
    employee_photo_url = ""

    if employee_id:
        try:
            db = SessionLocal()
            user = db.query(User).filter(User.employee_id == employee_id).first()
        except Exception:
            user = None
        finally:
            try:
                db.close()
            except Exception:
                pass

        if user and user.photo_blob:
            inline_images.append({
                "cid": photo_cid,
                "data": user.photo_blob,
                "maintype": "image",
                "subtype": (user.photo_mime or "image/jpeg").split("/")[-1]
            })
            employee_photo_url = f"cid:{photo_cid}"
    return {
        "employee_initial": employee_initial,
        "employee_photo_url": employee_photo_url
    }, inline_images


def send_welcome_email(to_email: str, name: str, employee_id: str, password: str) -> bool:
    subject = "Your TeamSync account details"
    body = (
        f"Hello {name},\n\n"
        "Your TeamSync account has been created.\n\n"
        f"User Name: {name}\n"
        f"Login ID: {employee_id}\n"
        f"Temporary Password: {password}\n\n"
        "Please log in and update your password after first login.\n\n"
        "Regards,\nTeamSync"
    )
    employee_context, inline_images = _get_employee_context(employee_id, name)
    context = {
        **_get_company_context(),
        **employee_context,
        "subject": subject,
        "employee_name": name,
        "employee_id": employee_id,
        "password": password
    }
    html_body = _render_template("email/welcome.html", context)
    return send_email(to_email, subject, body, html_body, inline_images)


def send_leave_requested_email(to_email: str, name: str, start_date: str, end_date: str, reason: str,
                               employee_id: Optional[str] = None) -> bool:
    subject = "Leave request submitted"
    body = (
        f"Hello {name},\n\n"
        "Your leave request has been submitted with the following details:\n\n"
        f"Status: Requested\n"
        f"Start Date: {start_date}\n"
        f"End Date: {end_date}\n"
        f"Reason: {reason}\n\n"
        "We will notify you once it is reviewed.\n\n"
        "Regards,\nTeamSync"
    )
    employee_context, inline_images = _get_employee_context(employee_id, name)
    context = {
        **_get_company_context(),
        **employee_context,
        "subject": subject,
        "employee_name": name,
        "status": "Requested",
        "start_date": start_date,
        "end_date": end_date,
        "reason": reason
    }
    html_body = _render_template("email/leave_status.html", context)
    return send_email(to_email, subject, body, html_body, inline_images)


def send_leave_status_email(to_email: str, name: str, start_date: str, end_date: str, reason: str, status: str,
                            employee_id: Optional[str] = None) -> bool:
    subject = f"Leave request {status}"
    body = (
        f"Hello {name},\n\n"
        "Your leave request has been updated:\n\n"
        f"Status: {status}\n"
        f"Start Date: {start_date}\n"
        f"End Date: {end_date}\n"
        f"Reason: {reason}\n\n"
        "Regards,\nTeamSync"
    )
    employee_context, inline_images = _get_employee_context(employee_id, name)
    context = {
        **_get_company_context(),
        **employee_context,
        "subject": subject,
        "employee_name": name,
        "status": status,
        "start_date": start_date,
        "end_date": end_date,
        "reason": reason
    }
    html_body = _render_template("email/leave_status.html", context)
    return send_email(to_email, subject, body, html_body, inline_images)


def send_meeting_invite(to_email: str, name: str, title: str, when: str, organizer: str,
                        link: Optional[str], employee_id: Optional[str] = None) -> bool:
    subject = f"Meeting invite: {title}"
    body = (
        f"Hello {name},\n\n"
        "You have been invited to a meeting.\n\n"
        f"Title: {title}\n"
        f"When: {when}\n"
        f"Organizer: {organizer}\n"
    )
    if link:
        body += f"Join link: {link}\n\n"
    else:
        body += "Join link: (not available yet)\n\n"

    body += "Regards,\nTeamSync"
    employee_context, inline_images = _get_employee_context(employee_id, name)
    context = {
        **_get_company_context(),
        **employee_context,
        "subject": subject,
        "employee_name": name,
        "meeting_title": title,
        "meeting_time": when,
        "organizer_name": organizer,
        "meeting_link": link or "#"
    }
    html_body = _render_template("email/meeting_invite.html", context)
    return send_email(to_email, subject, body, html_body, inline_images)


def send_bulk_meeting_invites(recipients: Iterable[dict], title: str, when: str, organizer: str, link: Optional[str]) -> None:
    if not _smtp_enabled():
        return
    for rec in recipients:
        send_meeting_invite(
            rec.get("email", ""),
            rec.get("name", ""),
            title,
            when,
            organizer,
            link,
            rec.get("employee_id")
        )