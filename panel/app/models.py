from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(32), unique=True, index=True, nullable=False)
    email = Column(String(128), unique=True, index=True, nullable=False)
    hashed_password = Column(String(256), nullable=False)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expire_at = Column(DateTime, nullable=True)

    instance = relationship("Instance", back_populates="user", uselist=False)


class Instance(Base):
    __tablename__ = "instances"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)

    astrbot_container_id = Column(String(64), nullable=True)
    napcat_container_id  = Column(String(64), nullable=True)

    astrbot_port    = Column(Integer, nullable=False)
    napcat_web_port = Column(Integer, nullable=False)
    napcat_ws_port  = Column(Integer, nullable=False)

    extra_ports_json = Column(Text, default="[]")

    status     = Column(String(16), default="creating")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="instance")


class SmtpConfig(Base):
    __tablename__ = "smtp_config"

    id         = Column(Integer, primary_key=True, default=1)
    host       = Column(String(256), default="")
    port       = Column(Integer, default=465)
    username   = Column(String(256), default="")
    password   = Column(String(256), default="")
    from_email = Column(String(256), default="")
    from_name  = Column(String(128), default="Bot Platform")
    use_tls    = Column(Boolean, default=True)
    enabled    = Column(Boolean, default=False)


class ServerNode(Base):
    __tablename__ = "server_nodes"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(64), nullable=False)
    url        = Column(String(256), nullable=False)
    api_token  = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class SiteConfig(Base):
    __tablename__ = "site_config"

    key   = Column(String(64), primary_key=True)
    value = Column(Text, default="")
