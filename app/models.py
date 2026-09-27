from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    niche: Mapped[str] = mapped_column(String(160))
    region: Mapped[str] = mapped_column(String(160), default="Nederland")
    search_query: Mapped[str] = mapped_column(String(300))
    target_count: Mapped[int] = mapped_column(Integer, default=25)
    status: Mapped[str] = mapped_column(String(40), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    leads: Mapped[list["Lead"]] = relationship(back_populates="campaign", cascade="all, delete-orphan")


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("domain", name="uq_leads_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    company_name: Mapped[str] = mapped_column(String(220))
    domain: Mapped[str] = mapped_column(String(220), index=True)
    website: Mapped[str] = mapped_column(String(500))
    address: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(String(80), default="google_places")
    status: Mapped[str] = mapped_column(String(40), default="new", index=True)
    score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    score_reasons: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    company_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    employee_signal: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    vacancies_signal: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    employer_branding_signal: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    event_signal: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    growth_signal: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    merch_signal: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    recommended_offer: Mapped[Optional[str]] = mapped_column(String(220), nullable=True)
    lead_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    contact_name: Mapped[Optional[str]] = mapped_column(String(180), nullable=True)
    contact_role: Mapped[Optional[str]] = mapped_column(String(180), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(220), nullable=True)
    email_source_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    outreach_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    crawl_excerpt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    campaign: Mapped[Campaign] = relationship(back_populates="leads")


class MailIntegration(Base):
    __tablename__ = "mail_integrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    account_id: Mapped[str] = mapped_column(String(120))
    email_address: Mapped[str] = mapped_column(String(220))
    refresh_token_encrypted: Mapped[str] = mapped_column(Text)
    connected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatCommand(Base):
    __tablename__ = "chat_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    action: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40), default="running")
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
