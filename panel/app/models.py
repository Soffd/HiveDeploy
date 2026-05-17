from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from zoneinfo import ZoneInfo
CST = ZoneInfo("Asia/Shanghai")
from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(32), unique=True, index=True, nullable=False)
    email = Column(String(128), unique=True, index=True, nullable=False)
    hashed_password = Column(String(256), nullable=False)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    expire_at = Column(DateTime, nullable=True)

    instance = relationship("Instance", back_populates="user", uselist=False)


class Instance(Base):
    __tablename__ = "instances"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)

    astrbot_container_id   = Column(String(64), nullable=True)
    napcat_container_id    = Column(String(64), nullable=True)
    llonebot_container_id  = Column(String(64), nullable=True)

    astrbot_port    = Column(Integer, nullable=False)
    napcat_web_port = Column(Integer, nullable=False)

    # bot_type: "napcat" 或 "llonebot"，标记当前部署的 QQ 机器人类型（互斥）
    bot_type = Column(String(16), default="napcat")

    # DB 列名保留 napcat_ws_port 确保零迁移兼容旧数据；
    # Python 侧统一用 astrbot_ws_port，含义：AstrBot 6199 对外映射端口。
    astrbot_ws_port = Column("napcat_ws_port", Integer, nullable=False)

    extra_ports_json = Column(Text, default="[]")

    status     = Column(String(16), default="creating")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", back_populates="instance")


class SmtpConfig(Base):
    __tablename__ = "smtp_config"

    id         = Column(Integer, primary_key=True, default=1)
    host       = Column(String(256), default="")
    port       = Column(Integer, default=465)
    username   = Column(String(256), default="")
    password   = Column(String(256), default="")
    from_email = Column(String(256), default="")
    from_name  = Column(String(128), default="HiveDeploy")
    use_tls    = Column(Boolean, default=True)
    enabled    = Column(Boolean, default=False)
    renewal_notify_email = Column(String(256), default="")


class ServerNode(Base):
    __tablename__ = "server_nodes"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(64), nullable=False)
    url        = Column(String(256), nullable=False)
    api_token  = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=datetime.now)


class SiteConfig(Base):
    __tablename__ = "site_config"

    key   = Column(String(64), primary_key=True)
    value = Column(Text, default="")


class Announcement(Base):
    """前台公告，多条竖列展示"""
    __tablename__ = "announcements"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(128), nullable=False, default="")
    content = Column(Text, nullable=False, default="")
    type = Column(String(32), nullable=False, default="info")
    level = Column(String(32), nullable=False, default="normal")
    enabled = Column(Boolean, default=True)
    pinned = Column(Boolean, default=False)
    font_size = Column(Integer, default=15)
    color = Column(String(16), default="")
    bold = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class EmailLog(Base):
    """邮件发送记录，防止重复发送"""
    __tablename__ = "email_log"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, nullable=False, index=True)
    email_type = Column(String(32), nullable=False)  # "expire_7","expire_3","expire_1","expire_0"
    sent_date  = Column(String(10), nullable=False)   # "2026-03-20"（CST日期）
    sent_at = Column(DateTime, default=datetime.now)


class EmailTemplate(Base):
    """可配置邮件模板"""
    __tablename__ = "email_templates"

    key = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    subject = Column(String(256), default="")
    body_html = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class VerificationCode(Base):
    """邮箱验证码"""
    __tablename__ = "verification_codes"

    id         = Column(Integer, primary_key=True, index=True)
    email      = Column(String(128), nullable=False, index=True)
    code       = Column(String(8), nullable=False)
    purpose    = Column(String(32), nullable=False)  # "register", "reset_password", "change_password"
    created_at = Column(DateTime, default=datetime.now)
    expires_at = Column(DateTime, nullable=False)
    used       = Column(Boolean, default=False)


class PaymentConfig(Base):
    """支付配置（单例 id=1）"""
    __tablename__ = "payment_config"

    id = Column(Integer, primary_key=True, default=1)
    wechat_qr = Column(Text, default="")
    alipay_qr = Column(Text, default="")
    price_text = Column(String(256), default="")
    instructions = Column(Text, default="")
    social_qq = Column(String(128), default="")
    social_wechat = Column(String(128), default="")
    social_telegram = Column(String(256), default="")
    social_discord = Column(String(256), default="")
    renewal_enabled = Column(Boolean, default=False)


class RenewalRecord(Base):
    """用户自助续期记录"""
    __tablename__ = "renewal_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    username = Column(String(32), nullable=False)
    days_added = Column(Integer, nullable=False)
    previous_expire_at = Column(DateTime, nullable=True)
    new_expire_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class BannedUser(Base):
    """全局封禁用户（通过 Hub 同步到所有节点）"""
    __tablename__ = "banned_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(32), nullable=False, index=True)
    email = Column(String(128), nullable=False, index=True)
    banned_at = Column(DateTime, default=datetime.now)
    source_node = Column(String(64), default="local")


class InviteCode(Base):
    """邀请码"""
    __tablename__ = "invite_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(32), unique=True, index=True, nullable=False)
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    used = Column(Boolean, default=False)
    used_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    used_at = Column(DateTime, nullable=True)
    hidden = Column(Boolean, default=False)
    source_node = Column(String(64), default="local")
    usage_synced = Column(Boolean, default=True)
