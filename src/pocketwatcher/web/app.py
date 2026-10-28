import csv
import io
import json
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from pocketwatcher import crypto
from pocketwatcher.alerts.dispatcher import Sender, make_sender
from pocketwatcher.alerts.links import read_mute_token
from pocketwatcher.alerts.targets import validate_target
from pocketwatcher.analytics import CSV_COLUMNS, PRODUCT_GROUPS, RANGES, build_report, period_for, short_day
from pocketwatcher.collectors.billing import load_costs
from pocketwatcher.databricks.client import DatabricksWarehouse
from pocketwatcher.db.models import AlertLog, Channel, DailyCost, Mute, Tenant, User, Workspace
from pocketwatcher.db.session import make_engine, make_sessionmaker
from pocketwatcher.onboarding.preflight import Check, PreflightResult, normalize_host, run_preflight
from pocketwatcher.onboarding.setup_sql import setup_steps
from pocketwatcher.onboarding.test_alert import send_test_alert
from pocketwatcher.rules.base import usd
from pocketwatcher.rules.config import RuleConfig
from pocketwatcher.settings import get_settings
from pocketwatcher.web.auth import AuthProvider, WorkOSAuth
from pocketwatcher.web.charts import GROUP_SLOT, burn_chart, column_chart, compact_usd, usd0
from pocketwatcher.web.summary import STATUS_LABELS, workspace_outlook
from pocketwatcher.worker import queue_sync, sync_refusal

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.filters["usd"] = usd
templates.env.filters["usd0"] = usd0  # the web UI's money format; alerts keep `usd`
templates.env.filters["compact"] = compact_usd
templates.env.filters["day"] = short_day  # "Oct 1"
templates.env.filters["day_time"] = lambda d: f"{short_day(d)}, {d:%H:%M}"  # "Oct 1, 08:46"
templates.env.globals["group_slot"] = GROUP_SLOT
templates.env.globals["product_labels"] = PRODUCT_GROUPS
templates.env.filters["plain"] = lambda v: f"{v:.2f}".rstrip("0").rstrip(".")
templates.env.globals["status_labels"] = STATUS_LABELS
# Alerts newer than this, on a first visit, are shown lit as new.
FIRST_VISIT_LIT = timedelta(hours=24)

Preflight = Callable[[str, str, str, str, date], PreflightResult]  # host, client id, secret, warehouse, today


def databricks_preflight(host: str, client_id: str, secret: str, warehouse_id: str, today: date) -> PreflightResult:
    try:
        wh = DatabricksWarehouse.connect(host, client_id, secret, warehouse_id)
    except Exception as exc:
        return PreflightResult(
            checks=[
                Check(
                    "Service principal login",
                    "fail",
                    str(exc)[:300],
                    "Check the workspace URL, client ID and OAuth secret.",
                )
            ]
        )
    return run_preflight(wh.client, wh, warehouse_id, host, today)


class NotSignedIn(Exception):
    pass


@dataclass
class BoardRow:
    workspace_id: int
    title: str
    severity: str
    impact_usd: float
    created_at: datetime
    sent: int = 0
    failed: int = 0
    error: str | None = None
    lit: bool = False


def create_app(
    sessions: sessionmaker | None = None,
    auth: AuthProvider | None = None,
    preflight: Preflight = databricks_preflight,
    sender: Sender | None = None,
) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="PocketWatcher")
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret or secrets.token_urlsafe(32),
        https_only=settings.secure_cookies,
        same_site="lax",
        max_age=14 * 24 * 3600,
    )
    state: dict[str, Any] = {"sessions": sessions, "auth": auth, "sender": sender}

    def get_db(request: Request) -> Iterator[Session]:
        if state["sessions"] is None:
            state["sessions"] = make_sessionmaker(make_engine())
        with state["sessions"]() as db:
            request.state.db = db  # lets render() build the sidebar for signed-in pages
            yield db

    def get_auth() -> AuthProvider:
        if state["auth"] is None:
            state["auth"] = WorkOSAuth()
        return state["auth"]

    def get_sender() -> Sender:
        if state["sender"] is None:
            state["sender"] = make_sender(httpx.Client(follow_redirects=False))
        return state["sender"]

    Db = Annotated[Session, Depends(get_db)]

    def current_user(request: Request, db: Db) -> User:
        user_id = request.session.get("user_id")
        user = db.get(User, user_id) if user_id else None
        if user is None:
            raise NotSignedIn
        return user

    CurrentUser = Annotated[User, Depends(current_user)]

    def csrf_token(request: Request) -> str:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(24)
        return request.session["csrf"]

    def check_csrf(request: Request, csrf: Annotated[str, Form()] = "") -> None:
        expected = request.session.get("csrf")
        if not expected or not secrets.compare_digest(csrf, expected):
            raise HTTPException(403, "Form expired; reload the page and try again.")

    Csrf = Depends(check_csrf)

    def render(request: Request, name: str, ctx: dict[str, Any] | None = None, status: int = 200) -> HTMLResponse:
        context = {"csrf": csrf_token(request), "flash": request.session.pop("flash", None), **(ctx or {})}
        user = context.get("user")
        db = getattr(request.state, "db", None)
        if user is not None and db is not None and "nav_workspaces" not in context:
            now = datetime.now(UTC)
            spaces = db.scalars(select(Workspace).where(Workspace.tenant_id == user.tenant_id).order_by(Workspace.name))
            context["nav_workspaces"] = [(w, workspace_outlook(db, w, now).status) for w in spaces]
        return templates.TemplateResponse(request, name, context, status_code=status)

    def redirect(
        url: str,
        flash: str | None = None,
        request: Request | None = None,
        *,
        error: bool = False,
        near: str | None = None,
    ) -> RedirectResponse:
        """`near` names the form a message belongs to, so the page can show it beside that form."""
        if flash and request is not None:
            request.session["flash"] = {"text": flash, "error": error, "near": near}
        if near:
            url = f"{url}#{near}"  # land on the form the message is about
        return RedirectResponse(url, status_code=303)

    def own_workspace(db: Session, user: User, workspace_id: int) -> Workspace:
        ws = db.get(Workspace, workspace_id)
        if ws is None or ws.tenant_id != user.tenant_id:
            raise HTTPException(404)  # 404, not 403: don't reveal other tenants' ids
        return ws

    def board(
        request: Request,
        db: Session,
        workspace_ids: list[int],
        now: datetime,
        mark_seen: bool = True,
        rank_by_cost: bool = False,
    ) -> list[BoardRow]:
        """Recent alerts, one row per finding (not per channel), newest first. Rows newer than this
        browser's previous look at the board are lit until it has been seen once."""
        if not workspace_ids:
            return []
        entries = db.scalars(
            select(AlertLog)
            .where(AlertLog.workspace_id.in_(workspace_ids))
            .order_by(AlertLog.created_at.desc())
            .limit(200)
        ).all()
        seen_raw = request.session.get("board_seen_at")
        seen = datetime.fromisoformat(seen_raw) if seen_raw else now - FIRST_VISIT_LIT
        if mark_seen:
            request.session["board_seen_at"] = now.isoformat()
        rows: dict[tuple[int, str, str], BoardRow] = {}
        for e in entries:
            created = e.created_at if e.created_at.tzinfo else e.created_at.replace(tzinfo=UTC)
            key = (e.workspace_id, e.fingerprint, e.title)
            row = rows.get(key)
            if row is None:
                if len(rows) == 25:
                    continue
                row = rows[key] = BoardRow(e.workspace_id, e.title, e.severity, e.impact_usd, created)
            row.sent += e.status == "sent"
            row.failed += e.status == "failed"
            if e.error and not row.error:
                row.error = e.error
        for row in rows.values():
            row.lit = row.created_at > seen
        if rank_by_cost:
            return sorted(rows.values(), key=lambda r: (r.severity == "info", -r.impact_usd))
        return list(rows.values())

    @app.exception_handler(NotSignedIn)
    def _signed_out(request: Request, exc: NotSignedIn) -> Response:
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # --- auth ---------------------------------------------------------------

    @app.get("/login")
    def login(request: Request) -> RedirectResponse:
        request.session["oauth_state"] = state_value = secrets.token_urlsafe(24)
        url = get_auth().authorization_url(state_value, f"{settings.app_base_url.rstrip('/')}/callback")
        return RedirectResponse(url, status_code=303)

    @app.get("/callback")
    def callback(request: Request, db: Db, code: str = "", state: str = "") -> RedirectResponse:
        expected = request.session.pop("oauth_state", None)
        if not code or not expected or not secrets.compare_digest(state, expected):
            raise HTTPException(400, "Sign-in expired; please try again.")
        identity = get_auth().authenticate(code)
        user = db.scalars(select(User).where(User.auth_user_id == identity.user_id)).first()
        if user is None:
            # Each new sign-up gets its own tenant. Joining a teammate's tenant will be by invite,
            # never by matching email domains.
            tenant = Tenant(name=identity.email.split("@")[-1])
            db.add(tenant)
            db.flush()
            user = User(tenant_id=tenant.id, auth_user_id=identity.user_id, email=identity.email, name=identity.name)
            db.add(user)
            db.commit()
        request.session.clear()
        request.session["user_id"] = user.id
        if identity.session_id:
            request.session["auth_session_id"] = identity.session_id
        return redirect("/")

    @app.post("/logout", dependencies=[Csrf])
    def logout(request: Request) -> RedirectResponse:
        auth_session_id = request.session.get("auth_session_id")
        request.session.clear()
        if not auth_session_id:
            return redirect("/")
        # End the identity provider's session too, then come back to the signed-out home page.
        return RedirectResponse(
            get_auth().logout_url(auth_session_id, settings.app_base_url.rstrip("/") + "/"), status_code=303
        )

    # --- dashboard ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, db: Db) -> HTMLResponse:
        try:
            user = current_user(request, db)
        except NotSignedIn:
            return render(request, "landing.html")
        workspaces = db.scalars(select(Workspace).where(Workspace.tenant_id == user.tenant_id)).all()
        now = datetime.now(UTC)
        has_channels = db.scalars(select(Channel.id).where(Channel.tenant_id == user.tenant_id)).first() is not None
        return render(
            request,
            "dashboard.html",
            {
                "user": user,
                "passes": [(w, workspace_outlook(db, w, now)) for w in workspaces],
                "board": board(request, db, [w.id for w in workspaces], now, rank_by_cost=True),
                "has_channels": has_channels,
                "names": {w.id: w.name for w in workspaces},
            },
        )

    # --- connect a workspace ------------------------------------------------

    @app.get("/workspaces/new", response_class=HTMLResponse)
    def workspace_new(request: Request, user: CurrentUser, app_id: str = "") -> HTMLResponse:
        steps, error = None, None
        if app_id:
            try:
                steps = setup_steps(app_id)
            except ValueError as exc:
                error = str(exc)
        return render(
            request, "workspace_new.html", {"user": user, "app_id": app_id, "steps": steps, "error": error, "form": {}}
        )

    @app.post("/workspaces", response_class=HTMLResponse, dependencies=[Csrf])
    def workspace_create(
        request: Request,
        db: Db,
        user: CurrentUser,
        name: Annotated[str, Form()],
        host: Annotated[str, Form()],
        client_id: Annotated[str, Form()],
        client_secret: Annotated[str, Form()],
        warehouse_id: Annotated[str, Form()],
    ) -> Response:
        form = {"name": name, "host": host, "client_id": client_id, "warehouse_id": warehouse_id}
        ctx: dict[str, Any] = {"user": user, "app_id": client_id, "steps": None, "error": None, "form": form}
        try:
            host = normalize_host(host)
            ctx["steps"] = setup_steps(client_id.strip())
        except ValueError as exc:
            return render(request, "workspace_new.html", {**ctx, "error": str(exc)}, status=400)

        result = preflight(host, client_id.strip(), client_secret, warehouse_id.strip(), datetime.now(UTC).date())
        if not result.ok or result.workspace_id is None:
            # The secret is never echoed back or stored when preflight fails.
            return render(request, "workspace_new.html", {**ctx, "preflight": result}, status=422)
        # A second copy would sync twice: every alert sent twice, twice the warehouse time.
        existing = db.scalars(
            select(Workspace).where(
                Workspace.tenant_id == user.tenant_id,
                or_(Workspace.databricks_workspace_id == result.workspace_id, Workspace.host == host),
            )
        ).first()
        if existing is not None:
            return redirect(
                f"/workspaces/{existing.id}",
                f"This Databricks workspace is already connected as '{existing.name}'.",
                request,
                error=True,
            )

        ws = Workspace(
            tenant_id=user.tenant_id,
            name=name.strip() or host,
            host=host,
            databricks_workspace_id=result.workspace_id,
            cloud=result.cloud or "AWS",
            client_id=client_id.strip(),
            client_secret_enc=crypto.encrypt(client_secret),
            warehouse_id=warehouse_id.strip(),
            rule_config=RuleConfig().model_dump(),
        )
        db.add(ws)
        db.commit()
        warnings = [c for c in result.checks if c.status == "warn"]
        note = f" {len(warnings)} warning{'s' if len(warnings) > 1 else ''} to review below." if warnings else ""
        request.session["preflight"] = [c.__dict__ for c in result.checks]
        return redirect(
            f"/workspaces/{ws.id}", f"Connected. The first billing sync starts within a minute.{note}", request
        )

    @app.get("/workspaces/{workspace_id}", response_class=HTMLResponse)
    def workspace_detail(request: Request, db: Db, user: CurrentUser, workspace_id: int) -> HTMLResponse:
        ws = own_workspace(db, user, workspace_id)
        cfg = RuleConfig.model_validate(ws.rule_config or {})
        checks = request.session.pop("preflight", None)
        # A rejected settings form comes back with what was typed and which field was wrong.
        rejected = request.session.pop("settings_form", None)
        now = datetime.now(UTC)
        ctx = {
            "user": user,
            "ws": ws,
            "cfg": cfg,
            "outlook": workspace_outlook(db, ws, now),
            # One workspace's history doesn't count as having seen the whole board.
            "board": board(request, db, [ws.id], now, mark_seen=False),
            "checks": checks,
            "sync_block": sync_refusal(ws, now),
            "rejected": rejected,
        }
        return render(request, "workspace.html", ctx)

    @app.post("/workspaces/{workspace_id}/sync", dependencies=[Csrf])
    def workspace_sync(request: Request, db: Db, user: CurrentUser, workspace_id: int) -> RedirectResponse:
        # Only marks the work due; the worker runs it, so a slow sync never ties up a web request.
        ws = own_workspace(db, user, workspace_id)
        now = datetime.now(UTC)
        refusal = sync_refusal(ws, now)
        if refusal:
            return redirect(f"/workspaces/{ws.id}", refusal, request, error=True)
        queue_sync(ws, now)
        db.commit()
        return redirect(
            f"/workspaces/{ws.id}",
            "Sync queued. The worker picks it up within a minute if it is running; refresh to see the result.",
            request,
        )

    @app.post("/workspaces/{workspace_id}/settings", dependencies=[Csrf])
    def workspace_settings(
        request: Request,
        db: Db,
        user: CurrentUser,
        workspace_id: int,
        daily_usd: Annotated[str, Form()] = "",
        monthly_usd: Annotated[str, Form()] = "",
        required_tags: Annotated[str, Form()] = "",
        dev_tag_values: Annotated[str, Form()] = "",
        discount_percent: Annotated[str, Form()] = "0",
    ) -> RedirectResponse:
        ws = own_workspace(db, user, workspace_id)
        cfg = RuleConfig.model_validate(ws.rule_config or {})
        submitted = {
            "daily_usd": daily_usd,
            "monthly_usd": monthly_usd,
            "required_tags": required_tags,
            "dev_tag_values": dev_tag_values,
            "discount_percent": discount_percent,
        }
        field: str | None = None  # the input being read when a value is rejected
        try:
            field = "monthly_usd"
            cfg.budget.monthly_usd = _money(monthly_usd)
            field = "daily_usd"
            cfg.budget.daily_usd = _money(daily_usd)
            field = "discount_percent"
            discount = _number(discount_percent or "0")
            if not 0 <= discount < 100:
                raise ValueError("Discount must be between 0 and 99%.")
            field = "required_tags"
            cfg.untagged.required_tags = _list(required_tags)
            field = "dev_tag_values"
            if dev_tag_values.strip():
                cfg.dev_premium.tag_values = _list(dev_tag_values)
            field = None
            cfg = RuleConfig.model_validate(cfg.model_dump())
        except (ValueError, ValidationError) as exc:
            message = str(exc)
            request.session["settings_form"] = {"values": submitted, "field": field, "error": message}
            return redirect(f"/workspaces/{ws.id}", f"Not saved: {message}", request, error=True, near="settings")
        ws.rule_config = cfg.model_dump()
        ws.discount_multiplier = 1 - discount / 100
        db.commit()
        return redirect(f"/workspaces/{ws.id}", "Settings saved.", request, near="settings")

    @app.post("/workspaces/{workspace_id}/delete", dependencies=[Csrf])
    def workspace_delete(
        request: Request, db: Db, user: CurrentUser, workspace_id: int, confirm: Annotated[str, Form()] = ""
    ) -> RedirectResponse:
        ws = own_workspace(db, user, workspace_id)
        if confirm.strip() != ws.name:
            return redirect(
                f"/workspaces/{ws.id}",
                "Not removed: type the workspace name exactly.",
                request,
                error=True,
                near="remove",
            )
        name, client_id = ws.name, ws.client_id
        remove_workspace(db, ws)
        return redirect(
            "/",
            f"Removed '{name}' and its cost and alert history. PocketWatcher no longer connects to it. "
            f"To revoke access in Databricks too, delete the service principal {client_id} there.",
            request,
        )

    # --- analytics ----------------------------------------------------------

    def analytics_scope(db: Session, user: User, ws: str) -> tuple[list[Workspace], list[Workspace]]:
        workspaces = list(db.scalars(select(Workspace).where(Workspace.tenant_id == user.tenant_id)))
        if not ws:
            return workspaces, workspaces
        return workspaces, [own_workspace(db, user, int(ws))] if ws.isdigit() else []

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics(
        request: Request, db: Db, user: CurrentUser, ws: str = "", range: str = "30d", tag: str = ""
    ) -> HTMLResponse:
        workspaces, scope = analytics_scope(db, user, ws)
        today = datetime.now(UTC).date()
        period = period_for(range, today)
        # Enough history for the comparison period and the current month's 7-day pace.
        start = min(period.previous().start, today.replace(day=1) - timedelta(days=7))
        rows = [r for w in scope for r in load_costs(db, w, start, today)]
        budgets = {w.id: RuleConfig.model_validate(w.rule_config or {}).budget.monthly_usd for w in scope}
        report = build_report(rows, period, today, tag_key=tag or None, budgets=budgets)
        ctx = {
            "user": user,
            "workspaces": workspaces,
            "names": {w.id: w.name for w in workspaces},
            "selected_ws": ws,
            "range_key": period.key,
            "ranges": RANGES,
            "report": report,
            "columns": column_chart(report.buckets, report.bucket_unit),
            "burn": burn_chart(report.burn) if report.burn else None,
            "has_data": any(w.billing_synced_at for w in scope),
        }
        return render(request, "analytics.html", ctx)

    @app.get("/analytics/export.csv")
    def analytics_export(db: Db, user: CurrentUser, ws: str = "", range: str = "30d") -> Response:
        workspaces, scope = analytics_scope(db, user, ws)
        today = datetime.now(UTC).date()
        period = period_for(range, today)
        names = {w.id: w.name for w in workspaces}
        buf = io.StringIO()
        out = csv.writer(buf)
        out.writerow(CSV_COLUMNS)
        rows = sorted(
            (r for w in scope for r in load_costs(db, w, period.start, period.end)),
            key=lambda r: (r.usage_date, names.get(r.workspace_id or 0, ""), -r.cost),
        )
        for r in rows:
            out.writerow(
                [
                    r.usage_date.isoformat(),
                    _cell(names.get(r.workspace_id or 0, "")),
                    r.product or "",
                    r.sku_name,
                    r.entity_type,
                    _cell(r.entity_id),
                    _cell(r.run_as or ""),
                    r.is_photon,
                    r.is_serverless,
                    _cell(json.dumps(r.tags, sort_keys=True)),
                    f"{r.cost:.4f}",
                ]
            )
        filename = f"pocketwatcher-costs-{period.start}-to-{period.end - timedelta(days=1)}.csv"
        return Response(
            buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # --- channels -----------------------------------------------------------

    @app.get("/channels", response_class=HTMLResponse)
    def channels(request: Request, db: Db, user: CurrentUser) -> HTMLResponse:
        rows = db.scalars(select(Channel).where(Channel.tenant_id == user.tenant_id).order_by(Channel.id)).all()
        workspaces = db.scalars(select(Workspace).where(Workspace.tenant_id == user.tenant_id)).all()
        return render(
            request,
            "channels.html",
            {"user": user, "channels": rows, "workspaces": workspaces, "names": {w.id: w.name for w in workspaces}},
        )

    @app.post("/channels", dependencies=[Csrf])
    def channel_create(
        request: Request,
        db: Db,
        user: CurrentUser,
        kind: Annotated[str, Form()],
        name: Annotated[str, Form()],
        target: Annotated[str, Form()],
        min_severity: Annotated[str, Form()] = "warning",
        workspace_id: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        try:
            target = validate_target(kind, target)
        except ValueError as exc:
            return redirect("/channels", str(exc), request, error=True, near="add-channel")
        if min_severity not in ("info", "warning", "critical"):
            raise HTTPException(400)
        ws_id = own_workspace(db, user, int(workspace_id)).id if workspace_id else None
        db.add(
            Channel(
                tenant_id=user.tenant_id,
                workspace_id=ws_id,
                name=name.strip() or kind,
                kind=kind,
                target_enc=crypto.encrypt(target),
                min_severity=min_severity,
            )
        )
        db.commit()
        return redirect("/channels", "Channel added. Send a test alert to check it.", request)

    def own_channel(db: Session, user: User, channel_id: int) -> Channel:
        ch = db.get(Channel, channel_id)
        if ch is None or ch.tenant_id != user.tenant_id:
            raise HTTPException(404)
        return ch

    @app.post("/channels/{channel_id}/test", dependencies=[Csrf])
    def channel_test(request: Request, db: Db, user: CurrentUser, channel_id: int) -> RedirectResponse:
        ch = own_channel(db, user, channel_id)
        ws = db.scalars(select(Workspace).where(Workspace.tenant_id == user.tenant_id)).first()
        demo = ws or Workspace(id=0, name="your workspace", host="https://databricks.com")
        try:
            send_test_alert(ch, demo, get_sender())
        except Exception as exc:
            return redirect("/channels", f"Test alert to '{ch.name}' failed: {str(exc)[:200]}", request, error=True)
        return redirect("/channels", f"Test alert sent to '{ch.name}'.", request)

    @app.post("/channels/{channel_id}/delete", dependencies=[Csrf])
    def channel_delete(request: Request, db: Db, user: CurrentUser, channel_id: int) -> RedirectResponse:
        ch = own_channel(db, user, channel_id)
        ch.enabled = False  # keep the row: alert history references it
        db.commit()
        return redirect("/channels", f"Channel '{ch.name}' removed.", request)

    # --- mute links ---------------------------------------------------------
    # Mute links arrive in Slack/Teams/email. Link unfurlers and mail scanners fetch URLs with
    # GET, so GET only shows a confirmation; the mute itself is a POST from that page. The
    # signed token is the authorization, so no sign-in is needed.

    @app.get("/mute", response_class=HTMLResponse)
    def mute_confirm(request: Request, db: Db, t: str = "") -> HTMLResponse:
        payload = read_mute_token(t) if t else None
        if payload is None:
            return render(request, "mute.html", {"state": "invalid"}, status=400)
        ws = db.get(Workspace, payload["ws"])
        ctx = {"state": "confirm", "token": t, "days": payload["days"], "workspace": ws}
        return render(request, "mute.html", {**ctx, "alert_title": _alert_title(db, payload)})

    @app.post("/mute", response_class=HTMLResponse)
    def mute_apply(request: Request, db: Db, t: Annotated[str, Form()]) -> HTMLResponse:
        payload = read_mute_token(t)
        if payload is None or db.get(Workspace, payload["ws"]) is None:
            return render(request, "mute.html", {"state": "invalid"}, status=400)
        until = datetime.now(UTC) + timedelta(days=payload["days"])
        db.add(Mute(workspace_id=payload["ws"], fingerprint=payload["fp"], until=until))
        db.commit()
        return render(request, "mute.html", {"state": "done", "until": until, "alert_title": _alert_title(db, payload)})

    return app


def _alert_title(db: Session, payload: dict[str, Any]) -> str | None:
    """The latest title sent for this finding, so the mute page can say what it mutes."""
    return db.scalar(
        select(AlertLog.title)
        .where(AlertLog.workspace_id == payload["ws"], AlertLog.fingerprint == payload["fp"])
        .order_by(AlertLog.created_at.desc())
        .limit(1)
    )


def _cell(value: str) -> str:
    """Neutralise spreadsheet formulas: tag values and owners come from customer workspaces."""
    return f"'{value}" if value[:1] in ("=", "+", "-", "@", chr(9), chr(13)) else value


def remove_workspace(db: Session, ws: Workspace) -> None:
    """Delete a workspace and everything that belongs only to it, in one transaction. Channels
    shared by all workspaces stay. A worker mid-task on it fails its writes and drops the task."""
    own_channels = select(Channel.id).where(Channel.workspace_id == ws.id)
    db.execute(delete(AlertLog).where(or_(AlertLog.workspace_id == ws.id, AlertLog.channel_id.in_(own_channels))))
    db.execute(delete(Mute).where(Mute.workspace_id == ws.id))
    db.execute(delete(DailyCost).where(DailyCost.workspace_id == ws.id))
    db.execute(delete(Channel).where(Channel.workspace_id == ws.id))
    db.delete(ws)
    db.commit()


def _number(value: str) -> float:
    try:
        return float(value.replace(",", "").replace("$", "").replace("%", "").strip())
    except ValueError:
        raise ValueError(f"'{value.strip()}' is not a number.") from None


def _money(value: str) -> float | None:
    if not value.replace(",", "").replace("$", "").strip():
        return None
    amount = _number(value)
    if amount <= 0:
        raise ValueError("Budgets must be positive.")
    return amount


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


app = create_app()
