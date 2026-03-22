import smtplib
import logging
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

logger = logging.getLogger(__name__)


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


def _expiry_html(username: str, days: int, expire_at: datetime) -> str:
    if days <= 0:
        title   = "⚠️ 您的 Bot 实例已到期"
        content = f"您的账号已于 <b>{expire_at.strftime('%Y-%m-%d')}</b> 到期，容器已被暂停。请联系管理员续期以恢复访问。"
    else:
        title   = f"⏰ Bot 实例将在 {days} 天后到期"
        content = f"您的账号将于 <b>{expire_at.strftime('%Y-%m-%d')}</b> 到期（剩余 <b>{days}</b> 天）。请及时联系管理员续期，避免访问中断。"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<body style="background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;padding:40px 20px;">
  <div style="max-width:500px;margin:0 auto;background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;">
    <div style="font-size:1.4rem;font-weight:700;color:#58a6ff;margin-bottom:16px;">🤖 Bot Platform</div>
    <h2 style="font-size:1.1rem;margin-bottom:12px;">{title}</h2>
    <p style="color:#8b949e;line-height:1.7;">你好，<b>{username}</b>，</p>
    <p style="color:#8b949e;line-height:1.7;">{content}</p>
    <p style="color:#484f58;font-size:.8rem;margin-top:24px;">此邮件由系统自动发送，请勿回复。</p>
  </div>
</body>
</html>"""


def check_and_enforce_expiry(db):
    """检查到期用户：停止容器 + 发送邮件提醒"""
    from .models import SmtpConfig, User
    from .docker_manager import stop_user_instance, start_user_instance

    try:
        smtp_cfg = db.query(SmtpConfig).filter_by(id=1).first()
        now = datetime.utcnow()
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
                    logger.info(f"已停止到期用户容器: {user.username}")
                except Exception as e:
                    logger.error(f"停止容器失败 {user.username}: {e}")

            # 发邮件提醒（7/3/1天前 + 到期当天）
            if user.email and smtp_cfg and smtp_cfg.enabled:
                if days in (7, 3, 1, 0):
                    subject = f"{'Bot 实例已到期' if days <= 0 else f'Bot 实例将在 {days} 天后到期'} — Bot Platform"
                    html    = _expiry_html(user.username, days, user.expire_at)
                    send_email(user.email, subject, html, smtp_cfg)

    except Exception as e:
        logger.error(f"到期检查出错: {e}")


def start_expiry_scheduler(get_db_func):
    def _job():
        db = next(get_db_func())
        try:
            check_and_enforce_expiry(db)
        finally:
            db.close()
        _schedule()

    def _schedule():
        t = threading.Timer(300, _job)   # 每5分钟检查一次
        t.daemon = True
        t.start()

    t0 = threading.Timer(60, _job)
    t0.daemon = True
    t0.start()
    logger.info("到期检查调度器已启动（每5分钟执行）")