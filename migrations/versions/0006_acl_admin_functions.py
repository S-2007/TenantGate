"""Atomic permission changes: SECURITY DEFINER admin functions + acl_change audit columns.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-11

The app role stays SELECT-only on documents/chunks/users. It gets EXECUTE on three narrow
functions instead. Each one runs as the owner, inside the CALLER's transaction, and:
  1. authorizes: the RLS context (app.tenant_id / app.user_id) must name a user who is, in the
     users table right now, an ACTIVE admin of that tenant (never trusts app.roles alone);
  2. locks and changes the target row(s): document + ALL its chunks in the same transaction, or
     the user row;
  3. writes an ``acl_change`` audit row with before/after JSON.
Everything commits together or nothing does. Errors use SQLSTATEs the API maps to HTTP codes:
  42501 -> 403 (not an admin), P0002 -> 404 (not in your tenant / not found),
  22023 or 23514 -> 422 (invalid change).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"

AUTHZ = """
    v_tenant := public.app_tenant_id();
    v_actor  := public.app_user_id();
    IF v_tenant IS NULL OR v_actor IS NULL OR NOT EXISTS (
        SELECT 1 FROM public.users au
        WHERE au.id = v_actor AND au.tenant_id = v_tenant AND au.is_active
          AND 'admin' = ANY (au.roles)
    ) THEN
        RAISE EXCEPTION 'admin of this tenant required' USING ERRCODE = '42501';
    END IF;
"""

SORTED = "ARRAY(SELECT DISTINCT x FROM unnest({a}) AS t(x) WHERE x IS NOT NULL ORDER BY x)"

SET_DOCUMENT_ACL = f"""
CREATE OR REPLACE FUNCTION admin_set_document_acl(
    p_doc text, p_classification doc_classification, p_roles text[], p_users uuid[],
    p_request_id uuid
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tenant uuid; v_actor uuid; d public.documents%ROWTYPE;
    v_class doc_classification; v_roles text[]; v_users uuid[];
    v_before jsonb; v_after jsonb; v_chunks integer;
BEGIN
{AUTHZ}
    SELECT * INTO d FROM public.documents WHERE id = p_doc AND tenant_id = v_tenant FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'document not found' USING ERRCODE = 'P0002';
    END IF;
    v_class := coalesce(p_classification, d.classification);
    v_roles := {SORTED.format(a="coalesce(p_roles, d.allowed_roles)")};
    v_users := ARRAY(SELECT y FROM (SELECT DISTINCT x AS y
                                    FROM unnest(coalesce(p_users, d.allowed_users)) AS t(x)
                                    WHERE x IS NOT NULL) AS s ORDER BY y::text)::uuid[];
    IF v_class IN ('public', 'internal') THEN
        -- lists are meaningless for these classes (D-018); drop them unless explicitly sent
        IF (p_roles IS NOT NULL AND cardinality(p_roles) > 0)
           OR (p_users IS NOT NULL AND cardinality(p_users) > 0) THEN
            RAISE EXCEPTION '% documents cannot have allowed_roles/allowed_users', v_class
                USING ERRCODE = '22023';
        END IF;
        v_roles := '{{}}'; v_users := '{{}}';
    END IF;
    IF EXISTS (SELECT 1 FROM unnest(v_users) AS t(x)
               WHERE NOT EXISTS (SELECT 1 FROM public.users tu
                                 WHERE tu.id = t.x AND tu.tenant_id = v_tenant)) THEN
        RAISE EXCEPTION 'allowed_users must be users of this tenant' USING ERRCODE = '22023';
    END IF;
    v_before := jsonb_build_object('classification', d.classification,
        'allowed_roles', to_jsonb(d.allowed_roles), 'allowed_users', to_jsonb(d.allowed_users),
        'acl_version', d.acl_version);
    IF v_class = d.classification AND v_roles = {SORTED.format(a="d.allowed_roles")}
       AND v_users = ARRAY(SELECT x FROM unnest(d.allowed_users) AS t(x) ORDER BY x::text)::uuid[]
    THEN
        RETURN jsonb_build_object('doc_id', p_doc, 'changed', false, 'before', v_before,
                                  'after', v_before, 'chunks_updated', 0);
    END IF;
    UPDATE public.documents
       SET classification = v_class, allowed_roles = v_roles, allowed_users = v_users,
           acl_version = d.acl_version + 1, updated_at = now()
     WHERE id = p_doc;
    UPDATE public.chunks
       SET classification = v_class, allowed_roles = v_roles, allowed_users = v_users,
           acl_version = d.acl_version + 1
     WHERE doc_id = p_doc;
    GET DIAGNOSTICS v_chunks = ROW_COUNT;
    v_after := jsonb_build_object('classification', v_class, 'allowed_roles', to_jsonb(v_roles),
        'allowed_users', to_jsonb(v_users), 'acl_version', d.acl_version + 1);
    INSERT INTO public.retrieval_audit (request_id, event_type, user_id, tenant_id, roles,
        status, target_type, target_id, change_before, change_after)
    SELECT p_request_id, 'acl_change', v_actor, v_tenant, a.roles, 'ok', 'document', p_doc,
           v_before, v_after
      FROM public.users a WHERE a.id = v_actor;
    RETURN jsonb_build_object('doc_id', p_doc, 'changed', true, 'before', v_before,
                              'after', v_after, 'chunks_updated', v_chunks);
END
$$
"""

SET_USER = f"""
CREATE OR REPLACE FUNCTION admin_set_user(
    p_user uuid, p_roles text[], p_active boolean, p_request_id uuid
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tenant uuid; v_actor uuid; v_user public.users%ROWTYPE;
    v_roles text[]; v_active boolean; v_before jsonb; v_after jsonb;
BEGIN
{AUTHZ}
    SELECT * INTO v_user FROM public.users WHERE id = p_user AND tenant_id = v_tenant FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'user not found' USING ERRCODE = 'P0002';
    END IF;
    v_roles := {SORTED.format(a="coalesce(p_roles, v_user.roles)")};
    v_active := coalesce(p_active, v_user.is_active);
    IF p_user = v_actor AND (NOT v_active OR NOT ('admin' = ANY (v_roles))) THEN
        RAISE EXCEPTION 'an admin cannot remove their own admin access' USING ERRCODE = '22023';
    END IF;
    v_before := jsonb_build_object('roles', to_jsonb(v_user.roles), 'is_active', v_user.is_active);
    v_after := jsonb_build_object('roles', to_jsonb(v_roles), 'is_active', v_active);
    IF v_before = v_after THEN
        RETURN jsonb_build_object('user_id', p_user, 'changed', false, 'before', v_before,
                                  'after', v_after);
    END IF;
    UPDATE public.users SET roles = v_roles, is_active = v_active WHERE id = p_user;
    INSERT INTO public.retrieval_audit (request_id, event_type, user_id, tenant_id, roles,
        status, target_type, target_id, change_before, change_after)
    SELECT p_request_id, 'acl_change', v_actor, v_tenant, a.roles, 'ok', 'user', p_user::text,
           v_before, v_after
      FROM public.users a WHERE a.id = v_actor;
    RETURN jsonb_build_object('user_id', p_user, 'changed', true, 'before', v_before,
                              'after', v_after);
END
$$
"""

FUNCS = (
    "admin_set_document_acl(text, doc_classification, text[], uuid[], uuid)",
    "admin_set_user(uuid, text[], boolean, uuid)",
)


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE retrieval_audit
            ADD COLUMN target_type text CHECK (target_type IS NULL
                                               OR target_type IN ('document', 'user')),
            ADD COLUMN target_id text,
            ADD COLUMN change_before jsonb,
            ADD COLUMN change_after jsonb
        """
    )
    op.execute(SET_DOCUMENT_ACL)
    op.execute(SET_USER)
    for fn in FUNCS:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {APP_ROLE}")


def downgrade() -> None:
    for fn in FUNCS:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")
    op.execute(
        """
        ALTER TABLE retrieval_audit
            DROP COLUMN IF EXISTS change_after,
            DROP COLUMN IF EXISTS change_before,
            DROP COLUMN IF EXISTS target_id,
            DROP COLUMN IF EXISTS target_type
        """
    )
