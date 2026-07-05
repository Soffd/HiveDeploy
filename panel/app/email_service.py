import smtplib
import logging
import threading
import os
import re
from html import escape
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DEFAULT_EMAIL_TEMPLATES = {
    "verification_register": {
        "name": "注册验证码",
        "subject": "HiveDeploy 注册验证码",
        "body_html": """<!DOCTYPE html>
<html lang="zh-CN">
<body style="background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;padding:40px 20px;">
  <div style="max-width:500px;margin:0 auto;background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;">
    <div style="font-size:1.4rem;font-weight:700;color:#58a6ff;margin-bottom:16px;">HiveDeploy</div>
    <h2 style="font-size:1.1rem;margin-bottom:12px;">注册邮箱验证码</h2>
    <p style="color:#8b949e;line-height:1.7;">您正在注册 HiveDeploy 账号，验证码是：</p>
    <div style="font-size:2rem;font-weight:700;color:#58a6ff;letter-spacing:8px;text-align:center;padding:16px 0;">{{code}}</div>
    <p style="color:#8b949e;line-height:1.7;">验证码 {{expires_minutes}} 分钟内有效，请勿泄露给他人。</p>
    <p style="color:#8b949e;line-height:1.7;"><a href="{{register_url}}" style="color:#58a6ff;">返回注册页面</a></p>
    <p style="color:#484f58;font-size:.8rem;margin-top:24px;">此邮件由系统自动发送，请勿回复。</p>
  </div>
</body>
</html>""",
    },
    "password_reset": {
        "name": "重置密码验证码",
        "subject": "HiveDeploy 重置密码验证码",
        "body_html": """<!DOCTYPE html>
<html lang="zh-CN">
<body style="background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;padding:40px 20px;">
  <div style="max-width:500px;margin:0 auto;background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;">
    <div style="font-size:1.4rem;font-weight:700;color:#58a6ff;margin-bottom:16px;">HiveDeploy</div>
    <h2 style="font-size:1.1rem;margin-bottom:12px;">重置密码验证码</h2>
    <p style="color:#8b949e;line-height:1.7;">您正在重置账号密码，验证码是：</p>
    <div style="font-size:2rem;font-weight:700;color:#58a6ff;letter-spacing:8px;text-align:center;padding:16px 0;">{{code}}</div>
    <p style="color:#8b949e;line-height:1.7;">验证码 {{expires_minutes}} 分钟内有效。</p>
    <p style="color:#8b949e;line-height:1.7;"><a href="{{reset_url}}" style="color:#58a6ff;">打开重置密码页面</a></p>
    <p style="color:#484f58;font-size:.8rem;margin-top:24px;">此邮件由系统自动发送，请勿回复。</p>
  </div>
</body>
</html>""",
    },
    "verification_change_password": {
        "name": "修改密码验证码",
        "subject": "HiveDeploy 修改密码验证码",
        "body_html": """<!DOCTYPE html>
<html lang="zh-CN">
<body style="background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;padding:40px 20px;">
  <div style="max-width:500px;margin:0 auto;background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;">
    <div style="font-size:1.4rem;font-weight:700;color:#58a6ff;margin-bottom:16px;">HiveDeploy</div>
    <h2 style="font-size:1.1rem;margin-bottom:12px;">修改密码验证码</h2>
    <p style="color:#8b949e;line-height:1.7;">您正在修改账号密码，验证码是：</p>
    <div style="font-size:2rem;font-weight:700;color:#58a6ff;letter-spacing:8px;text-align:center;padding:16px 0;">{{code}}</div>
    <p style="color:#8b949e;line-height:1.7;">验证码 {{expires_minutes}} 分钟内有效。</p>
    <p style="color:#8b949e;line-height:1.7;"><a href="{{profile_url}}" style="color:#58a6ff;">打开个人设置</a></p>
    <p style="color:#484f58;font-size:.8rem;margin-top:24px;">此邮件由系统自动发送，请勿回复。</p>
  </div>
</body>
</html>""",
    },
    "expiry_notice": {
        "name": "到期通知",
        "subject": "{{expiry_subject}} — HiveDeploy",
        "body_html": """<!DOCTYPE html>
<html lang="zh-CN">
<body style="background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;padding:40px 20px;">
  <div style="max-width:500px;margin:0 auto;background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;">
    <div style="font-size:1.4rem;font-weight:700;color:#58a6ff;margin-bottom:16px;">🤖 HiveDeploy</div>
    <h2 style="font-size:1.1rem;margin-bottom:12px;">{{expiry_title}}</h2>
    <p style="color:#8b949e;line-height:1.7;">你好，<b>{{username}}</b>，</p>
    <p style="color:#8b949e;line-height:1.7;">{{expiry_content}}</p>
    <div style="margin-top:20px;">
      <a href="{{renew_url}}" target="_blank" style="display:inline-block;background:#238636;color:#fff;text-decoration:none;font-weight:700;border-radius:8px;padding:10px 16px;margin:4px 8px 4px 0;">前往自助续期</a>
      <a href="{{dashboard_url}}" target="_blank" style="display:inline-block;background:#1f6feb;color:#fff;text-decoration:none;font-weight:700;border-radius:8px;padding:10px 16px;margin:4px 8px 4px 0;">查看控制台</a>
    </div>
    <p style="color:#8b949e;font-size:.82rem;line-height:1.7;margin-top:16px;">续期链接：<a href="{{renew_url}}" style="color:#58a6ff;">{{renew_url}}</a></p>
    <p style="color:#484f58;font-size:.8rem;margin-top:24px;">此邮件由系统自动发送，请勿回复。</p>
  </div>
</body>
</html>""",
    },
    "renewal_notice": {
        "name": "续期通知",
        "subject": "续期通知：{{username}} 自助续期 +{{days_added}} 天 — HiveDeploy",
        "body_html": """<!DOCTYPE html>
<html lang="zh-CN">
<body style="background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;padding:40px 20px;">
  <div style="max-width:560px;margin:0 auto;background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;">
    <div style="font-size:1.4rem;font-weight:700;color:#58a6ff;margin-bottom:16px;">🤖 HiveDeploy 续期通知</div>
    <h2 style="font-size:1.1rem;margin-bottom:12px;">用户自助续期</h2>
    <table style="width:100%;border-collapse:collapse;font-size:.9rem;color:#8b949e;">
      <tr><td style="padding:8px 0;border-bottom:1px solid #21262d;">服务器</td><td style="padding:8px 0;border-bottom:1px solid #21262d;color:#e6edf3;"><b>{{server_name}}</b></td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #21262d;">用户</td><td style="padding:8px 0;border-bottom:1px solid #21262d;color:#e6edf3;"><b>{{username}} #{{user_id}}</b></td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #21262d;">邮箱</td><td style="padding:8px 0;border-bottom:1px solid #21262d;">{{email}}</td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #21262d;">实例</td><td style="padding:8px 0;border-bottom:1px solid #21262d;">{{bot_type}} / AstrBot:{{astrbot_port}} / Bot:{{bot_port}}</td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #21262d;">续期时长</td><td style="padding:8px 0;border-bottom:1px solid #21262d;color:#3fb950;"><b>+{{days_added}} 天</b></td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #21262d;">续期时间</td><td style="padding:8px 0;border-bottom:1px solid #21262d;">{{renewal_time}}</td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #21262d;">原到期时间</td><td style="padding:8px 0;border-bottom:1px solid #21262d;">{{previous_expire_at}}</td></tr>
      <tr><td style="padding:8px 0;">新到期时间</td><td style="padding:8px 0;color:#58a6ff;"><b>{{new_expire_at}}</b></td></tr>
    </table>
    <div style="margin-top:20px;">
      <a href="{{admin_url}}" target="_blank" style="display:inline-block;background:#1f6feb;color:#fff;text-decoration:none;font-weight:700;border-radius:8px;padding:10px 16px;margin:4px 8px 4px 0;">打开管理后台核对</a>
      <a href="{{renewals_url}}" target="_blank" style="display:inline-block;background:#238636;color:#fff;text-decoration:none;font-weight:700;border-radius:8px;padding:10px 16px;margin:4px 8px 4px 0;">查看续期记录</a>
    </div>
    <p style="color:#8b949e;font-size:.82rem;line-height:1.7;margin-top:16px;">后台链接：<a href="{{admin_url}}" style="color:#58a6ff;">{{admin_url}}</a></p>
    <p style="color:#484f58;font-size:.8rem;margin-top:24px;">此邮件由系统自动发送，请勿回复。</p>
  </div>
</body>
</html>""",
    },
}

EMAIL_TEMPLATE_VARIABLES = [
    ("通用", "{{site_name}}", "站点名称，默认 HiveDeploy"),
    ("通用", "{{base_url}}", "面板网页根地址，由 PLATFORM_HOST 生成"),
    ("通用", "{{dashboard_url}}", "用户控制台链接"),
    ("通用", "{{renew_url}}", "自助续期链接"),
    ("通用", "{{login_url}}", "登录页链接"),
    ("通用", "{{username}}", "用户名"),
    ("通用", "{{email}}", "用户邮箱"),
    ("验证码", "{{code}}", "验证码"),
    ("验证码", "{{expires_minutes}}", "验证码有效分钟数"),
    ("验证码", "{{register_url}}", "注册页链接"),
    ("验证码", "{{reset_url}}", "重置密码页链接"),
    ("验证码", "{{profile_url}}", "个人设置页链接"),
    ("到期通知", "{{days}}", "剩余天数，0 表示当天到期"),
    ("到期通知", "{{expire_date}}", "到期日期"),
    ("到期通知", "{{expiry_subject}}", "到期邮件标题文本"),
    ("到期通知", "{{expiry_title}}", "到期邮件正文标题"),
    ("到期通知", "{{expiry_content}}", "到期说明正文"),
    ("续期通知", "{{server_name}}", "服务器名称或 PLATFORM_HOST"),
    ("续期通知", "{{user_id}}", "用户 ID"),
    ("续期通知", "{{bot_type}}", "用户当前 Bot 类型"),
    ("续期通知", "{{astrbot_port}}", "AstrBot 面板端口"),
    ("续期通知", "{{bot_port}}", "NapCat/LLOneBot WebUI 端口"),
    ("续期通知", "{{days_added}}", "本次续期天数"),
    ("续期通知", "{{renewal_time}}", "续期操作时间"),
    ("续期通知", "{{previous_expire_at}}", "原到期时间"),
    ("续期通知", "{{new_expire_at}}", "新到期时间"),
    ("续期通知", "{{admin_url}}", "管理后台资源列表链接"),
    ("续期通知", "{{renewals_url}}", "续期记录链接"),
]


def _site_base_url(db=None) -> str:
    """Return an absolute public URL for links embedded in email."""
    host = os.environ.get("PLATFORM_HOST", "localhost").strip()
    if host.startswith(("http://", "https://")):
        url = host
    else:
        scheme = "http" if host.startswith(("localhost", "127.", "0.0.0.0")) else "https"
        url = f"{scheme}://{host}"
    return url.rstrip("/")


def _email_button(label: str, href: str, color: str = "#238636") -> str:
    return (
        f'<a href="{escape(href, quote=True)}" target="_blank" '
        f'style="display:inline-block;background:{color};color:#fff;text-decoration:none;'
        f'font-weight:700;border-radius:8px;padding:10px 16px;margin:4px 8px 4px 0;">'
        f'{escape(label)}</a>'
    )


def common_email_context(db=None, user=None) -> dict:
    base_url = _site_base_url(db)
    return {
        "site_name": "HiveDeploy",
        "base_url": base_url,
        "dashboard_url": f"{base_url}/dashboard",
        "renew_url": f"{base_url}/renew",
        "login_url": f"{base_url}/login",
        "register_url": f"{base_url}/register",
        "reset_url": f"{base_url}/reset-password",
        "profile_url": f"{base_url}/profile",
        "username": getattr(user, "username", "") if user is not None else "",
        "email": getattr(user, "email", "") if user is not None else "",
    }


def template_key_for_verification(purpose: str) -> str:
    return {
        "register": "verification_register",
        "reset_password": "password_reset",
        "change_password": "verification_change_password",
    }.get(purpose, "verification_register")


def _render_placeholders(template: str, context: dict, escape_values: bool = True) -> str:
    def repl(match):
        key = match.group(1).strip()
        value = context.get(key, "")
        value = "" if value is None else str(value)
        return escape(value, quote=True) if escape_values else value
    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", repl, template or "")


def get_email_template(db, key: str) -> dict:
    default = DEFAULT_EMAIL_TEMPLATES[key]
    try:
        from .models import EmailTemplate
        row = db.query(EmailTemplate).filter_by(key=key).first() if db is not None else None
        if not row:
            return default.copy()
        return {
            "name": row.name or default["name"],
            "subject": row.subject or default["subject"],
            "body_html": row.body_html or default["body_html"],
        }
    except Exception:
        return default.copy()


def render_email_template(db, key: str, context: dict) -> tuple[str, str]:
    tpl = get_email_template(db, key)
    subject = _render_placeholders(tpl["subject"], context, escape_values=False)
    body = _render_placeholders(tpl["body_html"], context, escape_values=True)
    return subject, body


def send_email(to: str, subject: str, html_body: str, smtp_cfg) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{smtp_cfg.from_name} <{smtp_cfg.from_email}>"
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        if smtp_cfg.use_tls:
            server = smtplib.SMTP_SSL(smtp_cfg.host, smtp_cfg.port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_cfg.host, smtp_cfg.port, timeout=15)
            server.starttls()
        server.login(smtp_cfg.username, smtp_cfg.password)
        server.sendmail(smtp_cfg.from_email, [to], msg.as_string())
        server.quit()
        return True
    except Exception as e:
        logger.error(f"邮件发送失败 to={to}: {e}")
        return False


def _expiry_context(db, user, days: int, expire_at: datetime) -> dict:
    ctx = common_email_context(db, user)
    ctx.update({
        "days": days,
        "expire_date": expire_at.strftime("%Y-%m-%d") if expire_at else "",
    })
    if days <= 0:
        ctx["expiry_subject"] = "Bot 实例已到期"
        ctx["expiry_title"] = "⚠️ 您的 Bot 实例已到期"
        ctx["expiry_content"] = f"您的账号已于 {ctx['expire_date']} 到期，容器已被暂停。请前往自助续期页面或联系管理员恢复访问。"
    else:
        ctx["expiry_subject"] = f"Bot 实例将在 {days} 天后到期"
        ctx["expiry_title"] = f"⏰ Bot 实例将在 {days} 天后到期"
        ctx["expiry_content"] = f"您的账号将于 {ctx['expire_date']} 到期（剩余 {days} 天）。请及时续期，避免访问中断。"
    return ctx


def _already_sent(db, user_id: int, email_type: str, today: str) -> bool:
    """检查今天是否已经发过这封邮件"""
    from sqlalchemy import text
    try:
        result = db.execute(text(
            "SELECT COUNT(*) FROM email_log WHERE user_id=:uid AND email_type=:tp AND sent_date=:dt"
        ), {"uid": user_id, "tp": email_type, "dt": today}).scalar()
        return result > 0
    except Exception:
        return False


def _record_sent(db, user_id: int, email_type: str, today: str):
    """记录已发送"""
    from sqlalchemy import text
    try:
        db.execute(text(
            "INSERT INTO email_log (user_id, email_type, sent_date) VALUES (:uid, :tp, :dt)"
        ), {"uid": user_id, "tp": email_type, "dt": today})
        db.commit()
    except Exception as e:
        logger.error(f"记录邮件发送失败: {e}")


def _site_int(db, key: str, default: int, min_value: int = 0, max_value: int = 3650) -> int:
    from .models import SiteConfig
    try:
        cfg = db.query(SiteConfig).filter_by(key=key).first()
        value = int((cfg.value if cfg else str(default)) or default)
    except Exception:
        value = default
    return max(min_value, min(value, max_value))


def _cleanup_expired_users(db, now: datetime) -> int:
    """删除到期宽限期外的普通用户和实例，释放容量。"""
    from sqlalchemy import or_
    from .models import User, UserMessage, BannedUser
    from .docker_manager import delete_user_instance

    grace_days = _site_int(db, "auto_delete_expired_days", 7)
    cutoff = now - timedelta(days=grace_days)
    users = db.query(User).filter(
        User.is_admin == False,
        or_(User.retained_account == False, User.retained_account == None),
        User.expire_at != None,
        User.expire_at < cutoff,
    ).all()
    deleted = 0
    for user in users:
        try:
            delete_user_instance(user.username)
        except Exception as e:
            logger.error(f"删除过期用户实例失败 {user.username}: {e}")
        try:
            if user.instance:
                db.delete(user.instance)
            db.query(UserMessage).filter_by(user_id=user.id).delete(synchronize_session=False)
            banned = db.query(BannedUser).filter_by(username=user.username).first()
            if banned:
                db.delete(banned)
            db.delete(user)
            db.commit()
            deleted += 1
            logger.info(f"已自动删除到期超过 {grace_days} 天的用户: {user.username}")
        except Exception as e:
            db.rollback()
            logger.error(f"自动删除过期用户失败 {user.username}: {e}")
    return deleted


def check_and_enforce_expiry(db):
    """检查到期用户：停止容器、发送提醒，并按宽限期自动删除账号释放容量。"""
    from .models import SmtpConfig, User
    from .docker_manager import stop_user_instance

    try:
        smtp_cfg = db.query(SmtpConfig).filter_by(id=1).first()
        # 必须用 naive datetime.now()，与数据库中无时区的 expire_at 保持一致。
        # 若改为带时区的 aware datetime，与 naive expire_at 相减会抛 TypeError。
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        _cleanup_expired_users(db, now)

        users = db.query(User).filter(
            User.is_active == True,
            User.expire_at != None,
        ).all()

        for user in users:
            delta = user.expire_at - now
            days  = delta.days

            # 已到期 → 停止容器
            if days < 0:
                try:
                    stop_user_instance(user.username)
                except Exception as e:
                    logger.error(f"停止容器失败 {user.username}: {e}")

            # 发邮件提醒（7/3/1天前 + 到期当天，每天每类只发一次）
            if not (user.email and smtp_cfg and smtp_cfg.enabled):
                continue

            # 确定今天应发的邮件类型
            if days == 7:
                email_type = "expire_7"
            elif days == 3:
                email_type = "expire_3"
            elif days == 1:
                email_type = "expire_1"
            elif days == 0:
                email_type = "expire_0"
            else:
                continue  # 其他天数不发邮件

            # 检查今天是否已发过
            if _already_sent(db, user.id, email_type, today):
                logger.debug(f"今日已发送 {email_type} 给 {user.username}，跳过")
                continue

            subject, html = render_email_template(db, "expiry_notice", _expiry_context(db, user, days, user.expire_at))
            if send_email(user.email, subject, html, smtp_cfg):
                _record_sent(db, user.id, email_type, today)
                logger.info(f"已发送 {email_type} 提醒给 {user.username}")

    except Exception as e:
        logger.error(f"到期检查出错: {e}")


def send_verification_code(to_email: str, code: str, smtp_cfg, db=None, purpose: str = "register", user=None) -> bool:
    ctx = common_email_context(db, user)
    ctx.update({"email": to_email, "code": code, "expires_minutes": 10})
    subject, html = render_email_template(db, template_key_for_verification(purpose), ctx)
    return send_email(to_email, subject, html, smtp_cfg)


def _renewal_context(db, username: str, days_added: int, previous_expire_at, new_expire_at, renewal_time, user_obj=None, instance=None) -> dict:
    base_url = _site_base_url(db)
    server_name = os.environ.get("SITE_NAME") or os.environ.get("PLATFORM_HOST", "localhost")
    user_id = getattr(user_obj, "id", None)
    user_email = getattr(user_obj, "email", "") or ""
    bot_type = getattr(instance, "bot_type", "") or "napcat"
    astrbot_port = getattr(instance, "astrbot_port", "") or "-"
    bot_port = getattr(instance, "napcat_web_port", "") or "-"
    prev_str = previous_expire_at.strftime("%Y-%m-%d %H:%M") if previous_expire_at else "永久"
    new_str = new_expire_at.strftime("%Y-%m-%d %H:%M") if new_expire_at else "永久"
    time_str = renewal_time.strftime("%Y-%m-%d %H:%M")
    admin_url = f"{base_url}/admin#admin-resources"
    renewals_url = f"{base_url}/admin/renewals"
    ctx = common_email_context(db, user_obj)
    ctx.update({
        "server_name": server_name,
        "user_id": user_id or "",
        "email": user_email or "",
        "bot_type": bot_type,
        "astrbot_port": astrbot_port,
        "bot_port": bot_port,
        "days_added": days_added,
        "renewal_time": time_str,
        "previous_expire_at": prev_str,
        "new_expire_at": new_str,
        "admin_url": admin_url,
        "renewals_url": renewals_url,
        "username": username,
    })
    return ctx


def send_renewal_notification(db, username: str, days_added: int, previous_expire_at, new_expire_at, renewal_time) -> bool:
    """向管理员配置的续期通知邮箱发送自助续期通知"""
    from .models import SmtpConfig, User, Instance
    try:
        smtp_cfg = db.query(SmtpConfig).filter_by(id=1).first()
        if not smtp_cfg or not smtp_cfg.enabled:
            logger.debug("SMTP 未启用，跳过续期通知")
            return False
        notify_emails = smtp_cfg.renewal_notify_email.strip() if smtp_cfg.renewal_notify_email else ""
        if not notify_emails:
            logger.debug("未配置续期通知邮箱，跳过续期通知")
            return False
        user_obj = db.query(User).filter_by(username=username).first()
        instance = db.query(Instance).filter_by(user_id=user_obj.id).first() if user_obj else None
        subject, html = render_email_template(
            db,
            "renewal_notice",
            _renewal_context(db, username, days_added, previous_expire_at, new_expire_at, renewal_time, user_obj, instance),
        )
        emails = [e.strip() for e in notify_emails.split(",") if e.strip()]
        success_all = True
        for email in emails:
            if not send_email(email, subject, html, smtp_cfg):
                success_all = False
                logger.error(f"续期通知发送失败: {email}")
            else:
                logger.info(f"续期通知已发送至: {email}")
        return success_all
    except Exception as e:
        logger.error(f"发送续期通知出错: {e}")
        return False


def start_expiry_scheduler(get_db_func):
    def _job():
        db = next(get_db_func())
        try:
            check_and_enforce_expiry(db)
        finally:
            db.close()
        _schedule()

    def _schedule():
        t = threading.Timer(300, _job)
        t.daemon = True
        t.start()

    t0 = threading.Timer(60, _job)
    t0.daemon = True
    t0.start()
    logger.info("到期检查调度器已启动（每5分钟执行）")
