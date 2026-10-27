from datetime import date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map: ClassVar = {dict[str, Any]: JSON, list[Any]: JSON}


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    workspaces: Mapped[list["Workspace"]] = relationship(back_populates="tenant")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    auth_user_id: Mapped[str] = mapped_column(String(100), unique=True)  # identity provider's user id
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Workspace(Base):
    """A connected Databricks workspace, accessed as a read-only service principal."""

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(200))
    host: Mapped[str] = mapped_column(String(500))  # https://adb-123.4.azuredatabricks.net
    databricks_workspace_id: Mapped[int] = mapped_column(BigInteger)  # 16 digits on Azure
    cloud: Mapped[str] = mapped_column(String(10))  # AZURE | AWS | GCP
    client_id: Mapped[str] = mapped_column(String(100))
    client_secret_enc: Mapped[str] = mapped_column(String(2000))
    warehouse_id: Mapped[str] = mapped_column(String(100))
    # Applied to list prices to approximate negotiated rates (1.0 = list price).
    discount_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    # Serialized pocketwatcher.rules.config.RuleConfig.
    rule_config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    billing_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Worker schedule; due tasks are claimed with FOR UPDATE SKIP LOCKED.
    next_billing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_live_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant] = relationship(back_populates="workspaces")


class DailyCost(Base):
    """Estimated list cost per workspace, day and entity, aggregated from system.billing.usage."""

    __tablename__ = "daily_costs"
    __table_args__ = (Index("ix_daily_costs_ws_date", "workspace_id", "usage_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"))
    usage_date: Mapped[date] = mapped_column(Date)
    sku_name: Mapped[str] = mapped_column(String(200))
    product: Mapped[str | None] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[str] = mapped_column(String(200))
    run_as: Mapped[str | None] = mapped_column(String(300))
    tags: Mapped[dict[str, Any]] = mapped_column(default=dict)
    is_photon: Mapped[bool] = mapped_column(Boolean, default=False)
    is_serverless: Mapped[bool] = mapped_column(Boolean, default=False)
    quantity: Mapped[float] = mapped_column(Float)
    list_cost: Mapped[float] = mapped_column(Float)


class Channel(Base):
    """Where alerts go. A channel without a workspace receives alerts for all of the tenant's workspaces."""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id"))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10))  # slack | teams | email
    target_enc: Mapped[str] = mapped_column(String(4000))  # webhook URL or comma-separated addresses
    min_severity: Mapped[str] = mapped_column(String(10), default="warning")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Mute(Base):
    __tablename__ = "mutes"
    __table_args__ = (Index("ix_mutes_ws_fp", "workspace_id", "fingerprint"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"))
    fingerprint: Mapped[str] = mapped_column(String(500))
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AlertLog(Base):
    """One delivery attempt of a finding to a channel; the basis for dedupe, repeats and retries."""

    __tablename__ = "alert_log"
    __table_args__ = (Index("ix_alert_log_ws_fp", "workspace_id", "fingerprint", "channel_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    fingerprint: Mapped[str] = mapped_column(String(500))
    rule: Mapped[str] = mapped_column(String(50))
    severity: Mapped[str] = mapped_column(String(10))
    impact_usd: Mapped[float] = mapped_column(Float, default=0.0)
    title: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20))  # sent | failed
    error: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
