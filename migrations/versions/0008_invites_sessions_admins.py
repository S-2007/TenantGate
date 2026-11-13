# ruff: noqa: UP031  (plpgsql bodies use %-substitution; f-strings would clash with {} arrays)
"""Invites, session revocation (token_version), last-admin protection, security summary.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-12

Same pattern as 0006/0007: the app role gets no new table grants. Every new path is one
SECURITY DEFINER function that authorizes against the database and audits in its transaction.

- ``invites``: the admin invites an email with roles; only a SHA-256 hash of the one-time token
  is stored. ``accept_invite`` (public) takes the token hash + the new password hash, and is
  single-use, expiring and revocable. Invalid, used, revoked and expired tokens all raise the
  same error (TG410 -> HTTP 410), so a probe can't tell them apart.
- ``users.token_version``: copied into every JWT (claim ``tv``) and compared on every request.
  "Log out everywhere", a password change and an admin's "revoke sessions" bump it, so every
  older token stops working on its next use.
- ``admin_set_user`` now serialises role/status changes per tenant (advisory lock) and refuses
  any change that would leave the company without an active admin (TG409 -> HTTP 409). Before
  this, two admins demoting each other at the same moment could leave zero admins.
- ``security_summary(days)``: tenant-scoped aggregates of the audit log for the admin
  dashboard. Document titles are returned only when the admin may read the document.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"
ASSIGNABLE = "ARRAY['admin','employee','engineering','finance','hr','manager']::text[]"


def _m6():  # type: ignore[no-untyped-def]
    path = next(Path(__file__).parent.glob("0006_*.py"))
    spec = importlib.util.spec_from_file_location("m0006", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


ACTOR = """
    v_tenant := public.app_tenant_id();
    v_actor  := public.app_user_id();
    SELECT u.roles INTO v_roles FROM public.users u
     WHERE u.id = v_actor AND u.tenant_id = v_tenant AND u.is_active;
    IF v_tenant IS NULL OR v_actor IS NULL OR v_roles IS NULL THEN
        RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
    END IF;
"""
ADMIN = (
    ACTOR
    + """
    IF NOT ('admin' = ANY (v_roles)) THEN
        RAISE EXCEPTION 'admin of this tenant required' USING ERRCODE = '42501';
    END IF;
"""
)

CREATE_INVITE = """
CREATE OR REPLACE FUNCTION admin_create_invite(p_email text, p_roles text[], p_token_hash text,
    p_ttl_minutes integer, p_max_users integer, p_request_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_tenant uuid; v_actor uuid; v_roles text[]; v_new text[]; v_id uuid := gen_random_uuid();
        v_exp timestamptz := now() + make_interval(mins => p_ttl_minutes); v_email text := lower(p_email);
BEGIN
%(admin)s
    v_new := ARRAY(SELECT DISTINCT r FROM unnest(p_roles) AS t(r) WHERE r IS NOT NULL ORDER BY r);
    IF cardinality(v_new) = 0 OR NOT (v_new <@ %(assignable)s) THEN
        RAISE EXCEPTION 'roles must be chosen from the allowed list' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('users:' || v_tenant::text));
    IF EXISTS (SELECT 1 FROM public.users WHERE email = v_email) THEN
        RAISE EXCEPTION 'duplicate' USING ERRCODE = '23505';
    END IF;
    -- a new invite for the same email replaces the pending one
    UPDATE public.invites SET revoked_at = now()
     WHERE tenant_id = v_tenant AND email = v_email AND used_at IS NULL AND revoked_at IS NULL;
    IF (SELECT count(*) FROM public.users WHERE tenant_id = v_tenant)
       + (SELECT count(*) FROM public.invites WHERE tenant_id = v_tenant AND used_at IS NULL
             AND revoked_at IS NULL AND expires_at > now()) >= p_max_users THEN
        RAISE EXCEPTION 'user quota reached for this company' USING ERRCODE = '54000';
    END IF;
    INSERT INTO public.invites (id, tenant_id, email, roles, token_hash, created_by, expires_at)
         VALUES (v_id, v_tenant, v_email, v_new, p_token_hash, v_actor, v_exp);
    INSERT INTO public.retrieval_audit (request_id, event_type, user_id, tenant_id, roles, status,
        target_type, target_id, change_after)
    VALUES (p_request_id, 'invite_created', v_actor, v_tenant, v_roles, 'ok', 'invite', v_id::text,
            jsonb_build_object('roles', to_jsonb(v_new), 'expires_at', v_exp));
    RETURN jsonb_build_object('invite_id', v_id, 'email', v_email, 'roles', to_jsonb(v_new),
                              'expires_at', v_exp);
END $$
""" % {"admin": ADMIN, "assignable": ASSIGNABLE}

LIST_INVITES = """
CREATE OR REPLACE FUNCTION admin_list_invites() RETURNS TABLE (invite_id uuid, email text,
    roles text[], created_at timestamptz, expires_at timestamptz, status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT i.id, i.email, i.roles, i.created_at, i.expires_at,
           CASE WHEN i.used_at IS NOT NULL THEN 'accepted' WHEN i.revoked_at IS NOT NULL THEN 'revoked'
                WHEN i.expires_at <= now() THEN 'expired' ELSE 'pending' END
      FROM public.invites i
     WHERE i.tenant_id = public.app_tenant_id()
       AND EXISTS (SELECT 1 FROM public.users a WHERE a.id = public.app_user_id()
                   AND a.tenant_id = i.tenant_id AND a.is_active AND 'admin' = ANY (a.roles))
     ORDER BY i.created_at DESC
     LIMIT 200
$$
"""

REVOKE_INVITE = """
CREATE OR REPLACE FUNCTION admin_revoke_invite(p_id uuid, p_request_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_tenant uuid; v_actor uuid; v_roles text[];
BEGIN
%(admin)s
    UPDATE public.invites SET revoked_at = now()
     WHERE id = p_id AND tenant_id = v_tenant AND used_at IS NULL AND revoked_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'not found' USING ERRCODE = 'P0002';
    END IF;
    INSERT INTO public.retrieval_audit (request_id, event_type, user_id, tenant_id, roles, status,
        target_type, target_id)
    VALUES (p_request_id, 'invite_revoked', v_actor, v_tenant, v_roles, 'ok', 'invite', p_id::text);
    RETURN jsonb_build_object('invite_id', p_id, 'revoked', true);
END $$
""" % {"admin": ADMIN}

# The two public invite functions. Both take only the token HASH; every non-usable state gives
# the same TG410 so that "never existed", "used", "revoked" and "expired" look identical.
PREVIEW_INVITE = """
CREATE OR REPLACE FUNCTION invite_preview(p_token_hash text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v jsonb;
BEGIN
    SELECT jsonb_build_object('email', i.email, 'company', t.name, 'roles', to_jsonb(i.roles),
                              'expires_at', i.expires_at) INTO v
      FROM public.invites i JOIN public.tenants t ON t.id = i.tenant_id
     WHERE i.token_hash = p_token_hash AND i.used_at IS NULL AND i.revoked_at IS NULL
       AND i.expires_at > now();
    IF v IS NULL THEN
        RAISE EXCEPTION 'invite is invalid or has expired' USING ERRCODE = 'TG410';
    END IF;
    RETURN v;
END $$
"""

ACCEPT_INVITE = """
CREATE OR REPLACE FUNCTION accept_invite(p_token_hash text, p_password_hash text,
    p_max_users integer, p_request_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE i public.invites%ROWTYPE; v_user uuid := gen_random_uuid();
BEGIN
    SELECT * INTO i FROM public.invites
     WHERE token_hash = p_token_hash AND used_at IS NULL AND revoked_at IS NULL
       AND expires_at > now()
       FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invite is invalid or has expired' USING ERRCODE = 'TG410';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('users:' || i.tenant_id::text));
    IF (SELECT count(*) FROM public.users WHERE tenant_id = i.tenant_id) >= p_max_users THEN
        RAISE EXCEPTION 'user quota reached for this company' USING ERRCODE = '54000';
    END IF;
    INSERT INTO public.users (id, tenant_id, email, password_hash, roles, is_active)
         VALUES (v_user, i.tenant_id, i.email, p_password_hash, i.roles, true);
    UPDATE public.invites SET used_at = now(), accepted_user = v_user WHERE id = i.id;
    INSERT INTO public.retrieval_audit (request_id, event_type, user_id, tenant_id, roles, status,
        target_type, target_id)
    VALUES (p_request_id, 'invite_accepted', v_user, i.tenant_id, i.roles, 'ok', 'invite', i.id::text);
    RETURN jsonb_build_object('user_id', v_user, 'tenant_id', i.tenant_id, 'email', i.email,
                              'roles', to_jsonb(i.roles), 'token_version', 1);
END $$
"""

BUMP_MINE = """
CREATE OR REPLACE FUNCTION bump_my_token_version(p_new_password_hash text, p_request_id uuid)
    RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_tenant uuid; v_actor uuid; v_roles text[]; v_tv integer;
BEGIN
%(actor)s
    UPDATE public.users
       SET token_version = token_version + 1,
           password_hash = coalesce(p_new_password_hash, password_hash)
     WHERE id = v_actor
    RETURNING token_version INTO v_tv;
    INSERT INTO public.retrieval_audit (request_id, event_type, user_id, tenant_id, roles, status,
        target_type, target_id)
    VALUES (p_request_id, CASE WHEN p_new_password_hash IS NULL THEN 'sessions_revoked'
                               ELSE 'password_changed' END,
            v_actor, v_tenant, v_roles, 'ok', 'user', v_actor::text);
    RETURN jsonb_build_object('user_id', v_actor, 'token_version', v_tv);
END $$
""" % {"actor": ACTOR}

ADMIN_REVOKE_SESSIONS = """
CREATE OR REPLACE FUNCTION admin_revoke_sessions(p_user uuid, p_request_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_tenant uuid; v_actor uuid; v_roles text[]; v_tv integer;
BEGIN
%(admin)s
    UPDATE public.users SET token_version = token_version + 1
     WHERE id = p_user AND tenant_id = v_tenant
    RETURNING token_version INTO v_tv;
    IF v_tv IS NULL THEN
        RAISE EXCEPTION 'not found' USING ERRCODE = 'P0002';
    END IF;
    INSERT INTO public.retrieval_audit (request_id, event_type, user_id, tenant_id, roles, status,
        target_type, target_id)
    VALUES (p_request_id, 'sessions_revoked', v_actor, v_tenant, v_roles, 'ok', 'user', p_user::text);
    RETURN jsonb_build_object('user_id', p_user, 'token_version', v_tv);
END $$
""" % {"admin": ADMIN}

# Inserted into 0006's admin_set_user right before its UPDATE. The advisory lock serialises all
# role/status changes of one tenant, so the count below can't be raced.
LAST_ADMIN_GUARD = """
    PERFORM pg_advisory_xact_lock(hashtext('admins:' || v_tenant::text));
    IF (v_user.is_active AND 'admin' = ANY (v_user.roles))
       AND NOT (v_active AND 'admin' = ANY (v_roles))
       AND NOT EXISTS (SELECT 1 FROM public.users o WHERE o.tenant_id = v_tenant AND o.id <> p_user
                        AND o.is_active AND 'admin' = ANY (o.roles)) THEN
        RAISE EXCEPTION 'a company must keep at least one active admin' USING ERRCODE = 'TG409';
    END IF;
"""
UPDATE_MARK = "    UPDATE public.users SET roles = v_roles, is_active = v_active WHERE id = p_user;"

SUMMARY = """
CREATE OR REPLACE FUNCTION security_summary(p_days integer) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_tenant uuid; v_actor uuid; v_roles text[]; v_since timestamptz; v jsonb;
BEGIN
%(admin)s
    v_since := now() - make_interval(days => greatest(1, least(p_days, 90)));
    WITH a AS (
        SELECT * FROM public.retrieval_audit
         WHERE tenant_id = v_tenant AND ts >= v_since
    ), asks AS (SELECT * FROM a WHERE event_type = 'retrieval'),
    conf AS (
        SELECT x.doc_id, asks.user_id
          FROM asks CROSS JOIN LATERAL unnest(asks.returned_doc_ids) AS x(doc_id)
          JOIN public.documents d ON d.id = x.doc_id AND d.tenant_id = v_tenant
         WHERE d.classification = 'confidential'
    )
    SELECT jsonb_build_object(
        'days', greatest(1, least(p_days, 90)),
        'asks', (SELECT count(*) FROM asks),
        'asks_by_status', (SELECT coalesce(jsonb_object_agg(status, n), '{}'::jsonb)
                             FROM (SELECT status, count(*) n FROM asks GROUP BY status) s),
        'distinct_users', (SELECT count(DISTINCT user_id) FROM asks),
        'chunks_dropped_by_post_check', (SELECT coalesce(sum(cardinality(dropped_chunk_ids)), 0) FROM asks),
        'denied_by_user', (SELECT coalesce(jsonb_agg(r ORDER BY r->>'n' DESC), '[]'::jsonb) FROM (
            SELECT jsonb_build_object('email', u.email, 'n', count(*)) r
              FROM asks JOIN public.users u ON u.id = asks.user_id AND u.tenant_id = v_tenant
             WHERE asks.status <> 'ok' GROUP BY u.email ORDER BY count(*) DESC LIMIT 5) s),
        'confidential_documents', (SELECT coalesce(jsonb_agg(r ORDER BY (r->>'reads')::int DESC), '[]'::jsonb) FROM (
            SELECT jsonb_build_object(
                     'doc_id', c.doc_id,
                     'title', CASE WHEN public.app_can_read(d.tenant_id, d.classification,
                                   d.allowed_roles, d.allowed_users) THEN d.title END,
                     'reads', count(*), 'readers', count(DISTINCT c.user_id)) r
              FROM conf c JOIN public.documents d ON d.id = c.doc_id
             GROUP BY c.doc_id, d.tenant_id, d.classification, d.allowed_roles, d.allowed_users, d.title
             ORDER BY count(*) DESC LIMIT 5) s),
        'confidential_readers', (SELECT coalesce(jsonb_agg(r ORDER BY (r->>'reads')::int DESC), '[]'::jsonb) FROM (
            SELECT jsonb_build_object('email', u.email, 'reads', count(*), 'documents', count(DISTINCT c.doc_id)) r
              FROM conf c JOIN public.users u ON u.id = c.user_id AND u.tenant_id = v_tenant
             GROUP BY u.email ORDER BY count(*) DESC LIMIT 5) s),
        'uploads_by_day', (SELECT coalesce(jsonb_agg(r ORDER BY r->>'day'), '[]'::jsonb) FROM (
            SELECT jsonb_build_object('day', to_char(date_trunc('day', ts), 'YYYY-MM-DD'),
                     'uploaded', count(*) FILTER (WHERE event_type = 'document_uploaded' AND status = 'ok'),
                     'replaced', count(*) FILTER (WHERE event_type = 'document_replaced' AND status = 'ok'),
                     'deleted', count(*) FILTER (WHERE event_type = 'document_deleted'),
                     'failed', count(*) FILTER (WHERE event_type IN ('document_uploaded', 'document_replaced')
                                                AND status = 'error')) r
              FROM a WHERE event_type LIKE 'document_%%' GROUP BY date_trunc('day', ts)) s),
        'admin_events', (SELECT coalesce(jsonb_agg(r ORDER BY r->>'ts' DESC), '[]'::jsonb) FROM (
            SELECT jsonb_build_object('ts', a.ts, 'event', a.event_type, 'actor', u.email,
                                      'target_type', a.target_type, 'target_id', a.target_id) r
              FROM a LEFT JOIN public.users u ON u.id = a.user_id AND u.tenant_id = v_tenant
             WHERE a.event_type NOT IN ('retrieval') ORDER BY a.ts DESC LIMIT 15) s)
    ) INTO v;
    RETURN v;
END $$
""" % {"admin": ADMIN}

EVENTS = (
    "'retrieval','acl_change','tenant_registered','user_created','document_uploaded',"
    "'document_replaced','document_deleted','invite_created','invite_accepted','invite_revoked',"
    "'sessions_revoked','password_changed'"
)
EVENTS_0007 = (
    "'retrieval','acl_change','tenant_registered','user_created','document_uploaded',"
    "'document_replaced','document_deleted'"
)
FUNCS = (
    "admin_create_invite(text, text[], text, integer, integer, uuid)",
    "admin_list_invites()",
    "admin_revoke_invite(uuid, uuid)",
    "invite_preview(text)",
    "accept_invite(text, text, integer, uuid)",
    "bump_my_token_version(text, uuid)",
    "admin_revoke_sessions(uuid, uuid)",
    "security_summary(integer)",
)


def set_user_with_guard() -> str:
    """0006's admin_set_user with the last-admin guard inserted before its UPDATE."""
    src = _m6().SET_USER
    assert UPDATE_MARK in src, "0006 admin_set_user changed; update the guard insertion point"
    return src.replace(UPDATE_MARK, LAST_ADMIN_GUARD + UPDATE_MARK, 1)


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN token_version integer NOT NULL DEFAULT 1 "
               "CHECK (token_version >= 1)")  # fmt: skip
    op.execute(
        """
        CREATE TABLE invites (
            id            uuid PRIMARY KEY,
            tenant_id     uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            email         text NOT NULL CHECK (email = lower(email) AND length(email) <= 254),
            roles         text[] NOT NULL CHECK (cardinality(roles) > 0),
            token_hash    text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
            created_by    uuid NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            expires_at    timestamptz NOT NULL,
            used_at       timestamptz,
            revoked_at    timestamptz,
            accepted_user uuid
        )
        """
    )
    op.execute("CREATE INDEX ix_invites_tenant ON invites (tenant_id, created_at DESC)")
    # The app role has no privileges on invites at all: only the definer functions touch it.
    op.execute(f"REVOKE ALL ON invites FROM {APP_ROLE}")
    op.execute("ALTER TABLE invites ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invites FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE retrieval_audit DROP CONSTRAINT retrieval_audit_event_type_check")
    op.execute(f"ALTER TABLE retrieval_audit ADD CONSTRAINT retrieval_audit_event_type_check "
               f"CHECK (event_type IN ({EVENTS}))")  # fmt: skip
    op.execute("ALTER TABLE retrieval_audit DROP CONSTRAINT retrieval_audit_target_type_check")
    op.execute("ALTER TABLE retrieval_audit ADD CONSTRAINT retrieval_audit_target_type_check "
               "CHECK (target_type IS NULL OR target_type IN ('document', 'user', 'tenant', 'invite'))")  # fmt: skip
    op.execute(set_user_with_guard())
    for sql in (CREATE_INVITE, LIST_INVITES, REVOKE_INVITE, PREVIEW_INVITE, ACCEPT_INVITE,
                BUMP_MINE, ADMIN_REVOKE_SESSIONS, SUMMARY):  # fmt: skip
        op.execute(sql)
    for fn in FUNCS:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {APP_ROLE}")


def downgrade() -> None:
    for fn in FUNCS:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")
    op.execute(_m6().SET_USER)
    op.execute("ALTER TABLE retrieval_audit DROP CONSTRAINT retrieval_audit_target_type_check")
    op.execute(
        "ALTER TABLE retrieval_audit ADD CONSTRAINT retrieval_audit_target_type_check "
        "CHECK (target_type IS NULL OR target_type IN ('document', 'user', 'tenant'))"
    )
    op.execute("ALTER TABLE retrieval_audit DROP CONSTRAINT retrieval_audit_event_type_check")
    op.execute(f"ALTER TABLE retrieval_audit ADD CONSTRAINT retrieval_audit_event_type_check "
               f"CHECK (event_type IN ({EVENTS_0007}))")  # fmt: skip
    op.execute("DROP TABLE invites")
    op.execute("ALTER TABLE users DROP COLUMN token_version")
