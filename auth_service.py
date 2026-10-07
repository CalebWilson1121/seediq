from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any

from database import connect, rows_to_dicts

PBKDF2_ROUNDS = 210_000
SESSION_DAYS = 7


def _password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _temporary_password() -> str:
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(10))
    return f"SeedIQ-{core}!"


def login(email: str, password: str) -> dict[str, Any]:
    email = email.strip().lower()
    with connect() as conn:
        user = conn.execute(
            "SELECT u.*, o.name AS organization_name, o.status AS organization_status, o.access_enabled "
            "FROM platform_users u LEFT JOIN dealer_organizations o ON o.id=u.organization_id WHERE lower(u.email)=?",
            (email,),
        ).fetchone()
        if not user or user["status"] != "active":
            raise ValueError("Invalid email or password")
        expected = _password_hash(password, user["password_salt"])
        if not hmac.compare_digest(expected, user["password_hash"]):
            raise ValueError("Invalid email or password")
        if user["global_role"] != "super_admin" and user["organization_id"] is not None:
            if not bool(user["access_enabled"]) or user["organization_status"] not in ("active", "trial"):
                raise PermissionError("This dealer account is currently disabled")
        token = secrets.token_urlsafe(36)
        expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
        conn.execute(
            "INSERT INTO user_sessions(user_id,token_hash,expires_at) VALUES(?,?,?)",
            (user["id"], _token_hash(token), expires),
        )
        conn.execute("UPDATE platform_users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?", (user["id"],))
    return {
        "token": token,
        "expires_at": expires.isoformat(),
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "global_role": user["global_role"],
            "organization_id": user["organization_id"],
            "organization_name": user["organization_name"],
        },
    }


def logout(token: str | None) -> None:
    if not token:
        return
    with connect() as conn:
        conn.execute("DELETE FROM user_sessions WHERE token_hash=?", (_token_hash(token),))


def current_user(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT u.id,u.email,u.display_name,u.global_role,u.status,u.organization_id,"
            "o.name AS organization_name,o.status AS organization_status,o.access_enabled "
            "FROM user_sessions s JOIN platform_users u ON u.id=s.user_id "
            "LEFT JOIN dealer_organizations o ON o.id=u.organization_id "
            "WHERE s.token_hash=? AND s.expires_at>CURRENT_TIMESTAMP",
            (_token_hash(token),),
        ).fetchone()
        if not row or row["status"] != "active":
            return None
        if row["global_role"] != "super_admin" and row["organization_id"] is not None:
            if not bool(row["access_enabled"]) or row["organization_status"] not in ("active", "trial"):
                return None
        conn.execute("UPDATE user_sessions SET last_seen_at=CURRENT_TIMESTAMP WHERE token_hash=?", (_token_hash(token),))
    return dict(row)


def require_role(user: dict[str, Any] | None, *roles: str) -> dict[str, Any]:
    if not user:
        raise PermissionError("Login required")
    if roles and user.get("global_role") not in roles:
        raise PermissionError("You do not have permission for this action")
    return user


def admin_overview() -> dict[str, Any]:
    with connect() as conn:
        dealers = rows_to_dicts(conn.execute(
            "SELECT o.*,"
            "(SELECT COUNT(*) FROM platform_users u WHERE u.organization_id=o.id) AS user_count,"
            "(SELECT COUNT(*) FROM prospects p WHERE p.organization_id=o.id) AS prospect_count,"
            "(SELECT COUNT(*) FROM farms f WHERE f.organization_id=o.id) AS farm_count,"
            "(SELECT COUNT(*) FROM seed_catalogs c WHERE c.organization_id=o.id) AS catalog_count,"
            "(SELECT MAX(u.last_login_at) FROM platform_users u WHERE u.organization_id=o.id) AS last_activity "
            "FROM dealer_organizations o ORDER BY o.created_at DESC"
        ).fetchall())
        totals = conn.execute(
            "SELECT (SELECT COUNT(*) FROM dealer_organizations) AS dealers,"
            "(SELECT COUNT(*) FROM platform_users WHERE global_role<>'super_admin') AS users,"
            "(SELECT COUNT(*) FROM prospects) AS prospects,"
            "(SELECT COALESCE(SUM(total_acres),0) FROM prospects) AS prospect_acres"
        ).fetchone()
    return {"totals": dict(totals), "dealers": dealers}


def dealer_detail(organization_id: int) -> dict[str, Any]:
    with connect() as conn:
        org = conn.execute("SELECT * FROM dealer_organizations WHERE id=?", (organization_id,)).fetchone()
        if not org:
            raise KeyError("Dealer not found")
        users = rows_to_dicts(conn.execute(
            "SELECT u.id,u.email,u.display_name,u.global_role,u.status,u.last_login_at,u.created_at,COALESCE(dm.role,CASE WHEN u.global_role='dealer_admin' THEN 'dealer_admin' ELSE 'salesperson' END) AS dealer_role "
            "FROM platform_users u LEFT JOIN dealer_members dm ON lower(dm.email)=lower(u.email) AND dm.organization_id=u.organization_id "
            "WHERE u.organization_id=? ORDER BY u.display_name",
            (organization_id,),
        ).fetchall())
        farms = rows_to_dicts(conn.execute(
            "SELECT f.id,f.farm_name,f.producer_name,p.id AS prospect_id,p.status,p.total_acres "
            "FROM farms f LEFT JOIN prospects p ON p.farm_id=f.id WHERE f.organization_id=? ORDER BY f.updated_at DESC",
            (organization_id,),
        ).fetchall())
        catalogs = rows_to_dicts(conn.execute(
            "SELECT id,crop_year,catalog_name,brand,status,product_count,created_at FROM seed_catalogs WHERE organization_id=? ORDER BY crop_year DESC,created_at DESC",
            (organization_id,),
        ).fetchall())
    return {"dealer": dict(org), "users": users, "farms": farms, "catalogs": catalogs}


def set_dealer_access(organization_id: int, enabled: bool, actor_user_id: int) -> dict[str, Any]:
    with connect() as conn:
        org = conn.execute("SELECT id,name FROM dealer_organizations WHERE id=?", (organization_id,)).fetchone()
        if not org:
            raise KeyError("Dealer not found")
        conn.execute(
            "UPDATE dealer_organizations SET access_enabled=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (enabled, "active" if enabled else "suspended", organization_id),
        )
        conn.execute(
            "INSERT INTO admin_audit_log(actor_user_id,action,entity_type,entity_id,details_json) VALUES(?,?,?,?,?::jsonb)",
            (actor_user_id, "dealer_access_changed", "dealer_organization", str(organization_id), '{"enabled":' + ('true' if enabled else 'false') + '}'),
        )
    return {"organization_id": organization_id, "access_enabled": enabled, "status": "active" if enabled else "suspended"}


def set_user_access(user_id: int, enabled: bool, actor_user_id: int) -> dict[str, Any]:
    with connect() as conn:
        target = conn.execute("SELECT id,email,global_role FROM platform_users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise KeyError("User not found")
        if target["global_role"] == "super_admin":
            raise ValueError("Super admin access cannot be disabled from the dealer console")
        status = "active" if enabled else "disabled"
        conn.execute("UPDATE platform_users SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, user_id))
        conn.execute("UPDATE dealer_members SET status=?,updated_at=CURRENT_TIMESTAMP WHERE lower(email)=lower(?)", (status, target["email"]))
        if not enabled:
            conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
        conn.execute(
            "INSERT INTO admin_audit_log(actor_user_id,action,entity_type,entity_id,details_json) VALUES(?,?,?,?,?::jsonb)",
            (actor_user_id, "user_access_changed", "platform_user", str(user_id), '{"enabled":' + ('true' if enabled else 'false') + '}'),
        )
    return {"user_id": user_id, "status": status}


def dealer_team(organization_id: int) -> dict[str, Any]:
    with connect() as conn:
        org = conn.execute("SELECT id,name,max_seats FROM dealer_organizations WHERE id=?", (organization_id,)).fetchone()
        if not org:
            raise KeyError("Dealer not found")
        users = rows_to_dicts(conn.execute(
            "SELECT u.id,u.email,u.display_name,u.global_role,u.status,u.last_login_at,u.created_at,"
            "COALESCE(dm.role,CASE WHEN u.global_role='dealer_admin' THEN 'dealer_admin' ELSE 'salesperson' END) AS role "
            "FROM platform_users u LEFT JOIN dealer_members dm ON lower(dm.email)=lower(u.email) AND dm.organization_id=u.organization_id "
            "WHERE u.organization_id=? ORDER BY CASE WHEN u.global_role='dealer_admin' THEN 0 ELSE 1 END,u.display_name",
            (organization_id,),
        ).fetchall())
    return {"dealer": dict(org), "seat_count": len(users), "max_seats": org["max_seats"], "users": users}


def create_dealer_user(organization_id: int, email: str, display_name: str, role: str, actor_user_id: int) -> dict[str, Any]:
    role = (role or "salesperson").strip().lower()
    if role not in {"salesperson", "agronomist", "dealer_admin"}:
        raise ValueError("Role must be salesperson, agronomist, or dealer_admin")
    email = email.strip().lower()
    display_name = display_name.strip()
    if not email or "@" not in email:
        raise ValueError("A valid email is required")
    if not display_name:
        raise ValueError("Name is required")
    with connect() as conn:
        org = conn.execute("SELECT id,name,max_seats FROM dealer_organizations WHERE id=?", (organization_id,)).fetchone()
        if not org:
            raise KeyError("Dealer not found")
        existing = conn.execute("SELECT id FROM platform_users WHERE lower(email)=?", (email,)).fetchone()
        if existing:
            raise ValueError("A SeedIQ user with that email already exists")
        seats = int(conn.execute("SELECT COUNT(*) AS n FROM platform_users WHERE organization_id=?", (organization_id,)).fetchone()["n"] or 0)
        if org["max_seats"] is not None and seats >= int(org["max_seats"]):
            raise ValueError("This dealer has reached its licensed seat limit")
        temp_password = _temporary_password()
        salt = secrets.token_hex(16)
        password_hash = _password_hash(temp_password, salt)
        global_role = "dealer_admin" if role == "dealer_admin" else "dealer_user"
        user = conn.execute(
            "INSERT INTO platform_users(email,display_name,password_salt,password_hash,global_role,status,organization_id) VALUES(?,?,?,?,?,'active',?) RETURNING id,email,display_name,global_role,status,created_at",
            (email, display_name, salt, password_hash, global_role, organization_id),
        ).fetchone()
        conn.execute(
            "INSERT INTO dealer_members(organization_id,email,display_name,role,status,metadata_json) VALUES(?,?,?,?, 'active','{}'::jsonb) "
            "ON CONFLICT DO NOTHING",
            (organization_id, email, display_name, role),
        )
        conn.execute(
            "INSERT INTO admin_audit_log(actor_user_id,action,entity_type,entity_id,details_json) VALUES(?,?,?,?,?::jsonb)",
            (actor_user_id, "dealer_user_created", "platform_user", str(user["id"]), '{"role":"' + role + '"}'),
        )
    result = dict(user)
    result["role"] = role
    result["temporary_password"] = temp_password
    return result


def set_dealer_team_user_access(organization_id: int, user_id: int, enabled: bool, actor_user_id: int) -> dict[str, Any]:
    with connect() as conn:
        target = conn.execute("SELECT id,organization_id,global_role FROM platform_users WHERE id=?", (user_id,)).fetchone()
        if not target or int(target["organization_id"] or 0) != int(organization_id):
            raise KeyError("Dealer user not found")
        if int(target["id"]) == int(actor_user_id) and not enabled:
            raise ValueError("You cannot disable your own dealer admin account")
    return set_user_access(user_id, enabled, actor_user_id)


def dealer_demo_dashboard(organization_id: int) -> dict[str, Any]:
    with connect() as conn:
        org = conn.execute("SELECT id,name,status,access_enabled,license_end,max_seats FROM dealer_organizations WHERE id=?", (organization_id,)).fetchone()
        if not org:
            raise KeyError("Dealer not found")
        prospects = rows_to_dicts(conn.execute(
            "SELECT p.id,p.prospect_name,p.status,p.total_acres,p.metadata_json,f.id AS farm_id,f.producer_name "
            "FROM prospects p JOIN farms f ON f.id=p.farm_id WHERE p.organization_id=? "
            "ORDER BY CASE WHEN p.prospect_name ILIKE '%SeedIQ Demo%' THEN 0 ELSE 1 END, p.updated_at DESC",
            (organization_id,),
        ).fetchall())
        stats = conn.execute(
            "SELECT COUNT(*) AS prospects,COALESCE(SUM(total_acres),0) AS acres FROM prospects WHERE organization_id=?",
            (organization_id,),
        ).fetchone()
        catalog = conn.execute(
            "SELECT id,crop_year,catalog_name,status,product_count FROM seed_catalogs WHERE organization_id=? ORDER BY crop_year DESC,created_at DESC LIMIT 1",
            (organization_id,),
        ).fetchone()
    return {"dealer": dict(org), "stats": dict(stats), "prospects": prospects, "latest_catalog": dict(catalog) if catalog else None}
