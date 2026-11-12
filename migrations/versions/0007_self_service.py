# ruff: noqa: UP031  (plpgsql bodies use %-substitution; f-strings would clash with {} arrays)
"""Self-service tenants and document upload: narrow SECURITY DEFINER write paths + ready filter.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-12

The app role stays SELECT-only on tenants/users/documents/chunks. Every new write goes through
one function owned by the owner role, running inside the caller's transaction, which
authorizes against the database, enforces quotas, writes, and audits atomically:

  register_tenant          public (no identity yet): tenant + first admin + audit row
  admin_create_user        admin of the caller's tenant
  upload_document_begin    caller holds an upload role: documents row (status 'processing')
  upload_document_complete uploader or admin: chunks (ACL copied FROM the document row) +
                           status 'ready' (+ acl_version bump on replace), one transaction
  upload_document_fail     uploader or admin: status 'failed' / last_error, no chunks
  delete_document          admin or uploader: document + all chunks (cascade) + audit
  document_meta / my_uploads  metadata for the uploader or a reader (status page, own list)

Chunks of a document that is not 'ready' are invisible: the RLS policy on chunks now requires
the document to be ready (the WHERE clause and the post-check require the same).
Errors map to HTTP: 42501 -> 403, P0002 -> 404, 22023/23514 -> 422, 23505 -> 409, 54000 -> 409
(quota/cap).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"
ASSIGNABLE = "ARRAY['admin','employee','engineering','finance','hr','manager']::text[]"
EMPLOYEE = "ARRAY['admin','employee','engineering','finance','hr','manager']::text[]"
RESERVED = (
    "ARRAY['admin','api','www','health','metrics','docs','static','auth','tenants','root',"
    "'system','support','security','login','register','status','null','undefined']::text[]"
)

ACTOR = """
    v_tenant := public.app_tenant_id();
    v_actor  := public.app_user_id();
    SELECT u.roles INTO v_roles FROM public.users u
     WHERE u.id = v_actor AND u.tenant_id = v_tenant AND u.is_active;
    IF v_tenant IS NULL OR v_actor IS NULL OR v_roles IS NULL THEN
        RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
    END IF;
"""

# Same rule as can_access / the RLS policies, for one document row and the caller.
CAN_READ = f"""
CREATE OR REPLACE FUNCTION app_can_read(p_tenant uuid, p_class doc_classification,
                                        p_roles text[], p_users uuid[]) RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT p_tenant = public.app_tenant_id() AND (
        p_class = 'public'
        OR (p_class = 'internal' AND public.app_roles() && {EMPLOYEE})
        OR (p_class = 'confidential'
            AND (p_roles && public.app_roles() OR public.app_user_id() = ANY (p_users))))
$$
"""

REGISTER = f"""
CREATE OR REPLACE FUNCTION register_tenant(p_slug text, p_name text, p_email text,
    p_password_hash text, p_max_tenants integer, p_request_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_tenant uuid := gen_random_uuid(); v_user uuid := gen_random_uuid();
BEGIN
    -- serialise registrations so the tenant cap can't be raced past
    PERFORM pg_advisory_xact_lock(hashtext('register_tenant'));
    IF (SELECT count(*) FROM public.tenants) >= p_max_tenants THEN
        RAISE EXCEPTION 'tenant limit reached' USING ERRCODE = '54000';
    END IF;
    IF p_slug = ANY ({RESERVED}) THEN
        RAISE EXCEPTION 'this company slug is reserved' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.tenants (id, slug, name, source) VALUES (v_tenant, p_slug, p_name, 'self_service');
    INSERT INTO public.users (id, tenant_id, email, password_hash, roles, is_active)
         VALUES (v_user, v_tenant, lower(p_email), p_password_hash, ARRAY['admin'], true);
    INSERT INTO public.retrieval_audit (request_id, event_type, user_id, tenant_id, roles, status,
        target_type, target_id, change_after)
    VALUES (p_request_id, 'tenant_registered', v_user, v_tenant, ARRAY['admin'], 'ok', 'tenant',
            p_slug, jsonb_build_object('name', p_name));
    RETURN jsonb_build_object('tenant_id', v_tenant, 'user_id', v_user);
END $$
"""

CREATE_USER = f"""
CREATE OR REPLACE FUNCTION admin_create_user(p_email text, p_roles text[], p_password_hash text,
    p_max_users integer, p_request_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_tenant uuid; v_actor uuid; v_roles text[]; v_user uuid := gen_random_uuid();
        v_new text[];
BEGIN
{ACTOR}
    IF NOT ('admin' = ANY (v_roles)) THEN
        RAISE EXCEPTION 'admin of this tenant required' USING ERRCODE = '42501';
    END IF;
    v_new := ARRAY(SELECT DISTINCT r FROM unnest(p_roles) AS t(r) WHERE r IS NOT NULL ORDER BY r);
    IF cardinality(v_new) = 0 OR NOT (v_new <@ {ASSIGNABLE}) THEN
        RAISE EXCEPTION 'roles must be chosen from the allowed list' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('users:' || v_tenant::text));
    IF (SELECT count(*) FROM public.users WHERE tenant_id = v_tenant) >= p_max_users THEN
        RAISE EXCEPTION 'user quota reached for this company' USING ERRCODE = '54000';
    END IF;
    INSERT INTO public.users (id, tenant_id, email, password_hash, roles, is_active)
         VALUES (v_user, v_tenant, lower(p_email), p_password_hash, v_new, true);
    INSERT INTO public.retrieval_audit (request_id, event_type, user_id, tenant_id, roles, status,
        target_type, target_id, change_after)
    VALUES (p_request_id, 'user_created', v_actor, v_tenant, v_roles, 'ok', 'user', v_user::text,
            jsonb_build_object('roles', to_jsonb(v_new)));
    RETURN jsonb_build_object('user_id', v_user, 'roles', to_jsonb(v_new));
END $$
"""

BEGIN_UPLOAD = """
CREATE OR REPLACE FUNCTION upload_document_begin(p_doc_id text, p_title text,
    p_classification doc_classification, p_roles text[], p_users uuid[], p_upload_roles text[],
    p_max_docs integer, p_size bigint, p_content_type text, p_filename text, p_request_id uuid)
    RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_tenant uuid; v_actor uuid; v_roles text[];
        v_r text[]; v_u uuid[];
BEGIN
%(actor)s
    IF NOT (v_roles && p_upload_roles) THEN
        RAISE EXCEPTION 'your role may not upload documents' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('docs:' || v_tenant::text));
    IF (SELECT count(*) FROM public.documents WHERE tenant_id = v_tenant) >= p_max_docs THEN
        RAISE EXCEPTION 'document quota reached for this company' USING ERRCODE = '54000';
    END IF;
    v_r := ARRAY(SELECT DISTINCT r FROM unnest(coalesce(p_roles, '{}')) AS t(r)
                 WHERE r IS NOT NULL ORDER BY r);
    v_u := ARRAY(SELECT y FROM (SELECT DISTINCT x AS y FROM unnest(coalesce(p_users, '{}')) AS t(x)
                 WHERE x IS NOT NULL) s ORDER BY y::text)::uuid[];
    IF p_classification IN ('public', 'internal') THEN
        IF cardinality(v_r) > 0 OR cardinality(v_u) > 0 THEN
            RAISE EXCEPTION '%% documents cannot have allowed_roles/allowed_users', p_classification
                USING ERRCODE = '22023';
        END IF;
    ELSIF cardinality(v_r) = 0 AND cardinality(v_u) = 0 THEN
        RAISE EXCEPTION 'confidential documents need at least one allowed role or user'
            USING ERRCODE = '22023';
    END IF;
    -- every named user must be an ACTIVE user of THIS tenant (same message whatever the reason,
    -- so the error can't be used to discover users of other tenants)
    IF EXISTS (SELECT 1 FROM unnest(v_u) AS t(x) WHERE NOT EXISTS (
                 SELECT 1 FROM public.users u WHERE u.id = t.x AND u.tenant_id = v_tenant
                   AND u.is_active)) THEN
        RAISE EXCEPTION 'allowed_users must be active users of your company' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.documents (id, tenant_id, title, source_path, classification, allowed_roles,
        allowed_users, acl_version, status, uploaded_by, original_filename, content_type,
        size_bytes, source)
    VALUES (p_doc_id, v_tenant, p_title, 'upload:' || p_doc_id, p_classification, v_r, v_u, 1,
            'processing', v_actor, p_filename, p_content_type, p_size, 'upload');
    RETURN jsonb_build_object('doc_id', p_doc_id, 'tenant_id', v_tenant, 'classification',
        p_classification, 'allowed_roles', to_jsonb(v_r), 'allowed_users', to_jsonb(v_u),
        'acl_version', 1);
END $$
""" % {"actor": ACTOR}

# Shared authorisation for complete/fail/delete/meta: uploader or admin, same tenant.
MANAGE = """
    SELECT * INTO d FROM public.documents WHERE id = p_doc_id AND tenant_id = v_tenant FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'not found' USING ERRCODE = 'P0002';
    END IF;
    IF NOT ('admin' = ANY (v_roles) OR d.uploaded_by = v_actor) THEN
        RAISE EXCEPTION 'not found' USING ERRCODE = 'P0002';  -- uniform with "doesn't exist"
    END IF;
"""

COMPLETE = """
CREATE OR REPLACE FUNCTION upload_document_complete(p_doc_id text, p_chunks jsonb,
    p_max_chunks integer, p_replace boolean, p_size bigint, p_content_type text, p_filename text,
    p_request_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_tenant uuid; v_actor uuid; v_roles text[]; d public.documents%%ROWTYPE;
        v_n integer := jsonb_array_length(p_chunks); v_old integer; v_version integer;
BEGIN
%(actor)s
%(manage)s
    IF NOT p_replace AND d.status <> 'processing' THEN
        RAISE EXCEPTION 'document is not waiting for content' USING ERRCODE = '22023';
    END IF;
    IF p_replace AND d.status <> 'ready' THEN
        RAISE EXCEPTION 'only a ready document can be replaced' USING ERRCODE = '22023';
    END IF;
    IF v_n = 0 THEN
        RAISE EXCEPTION 'no text to index' USING ERRCODE = '22023';
    END IF;
    SELECT count(*) INTO v_old FROM public.chunks WHERE doc_id = p_doc_id;
    IF (SELECT count(*) FROM public.chunks WHERE tenant_id = v_tenant) - v_old + v_n > p_max_chunks THEN
        RAISE EXCEPTION 'chunk quota reached for this company' USING ERRCODE = '54000';
    END IF;
    v_version := d.acl_version + CASE WHEN p_replace THEN 1 ELSE 0 END;
    DELETE FROM public.chunks WHERE doc_id = p_doc_id;
    -- the ACL comes from the documents row, never from the caller
    INSERT INTO public.chunks (chunk_id, doc_id, tenant_id, title, chunk_index, text, embedding,
        classification, allowed_roles, allowed_users, acl_version)
    SELECT (c->>'chunk_id')::uuid, d.id, d.tenant_id, d.title, (c->>'chunk_index')::int,
           c->>'text', (c->>'embedding')::vector, d.classification, d.allowed_roles,
           d.allowed_users, v_version
      FROM jsonb_array_elements(p_chunks) AS c;
    UPDATE public.documents
       SET status = 'ready', acl_version = v_version, chunk_count = v_n, last_error = NULL,
           size_bytes = p_size, content_type = p_content_type, original_filename = p_filename,
           updated_at = now()
     WHERE id = p_doc_id;
    INSERT INTO public.retrieval_audit (request_id, event_type, user_id, tenant_id, roles, status,
        target_type, target_id, change_after)
    VALUES (p_request_id, CASE WHEN p_replace THEN 'document_replaced' ELSE 'document_uploaded' END,
            v_actor, v_tenant, v_roles, 'ok', 'document', p_doc_id,
            jsonb_build_object('bytes', p_size, 'chunks', v_n, 'content_type', p_content_type,
                               'acl_version', v_version));
    RETURN jsonb_build_object('doc_id', p_doc_id, 'status', 'ready', 'chunks', v_n,
                              'acl_version', v_version);
END $$
""" % {"actor": ACTOR, "manage": MANAGE}

FAIL = """
CREATE OR REPLACE FUNCTION upload_document_fail(p_doc_id text, p_error text, p_replace boolean,
    p_request_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_tenant uuid; v_actor uuid; v_roles text[]; d public.documents%%ROWTYPE;
BEGIN
%(actor)s
%(manage)s
    IF NOT p_replace THEN
        DELETE FROM public.chunks WHERE doc_id = p_doc_id;  -- a failed upload has no chunks
        UPDATE public.documents SET status = 'failed', last_error = left(p_error, 300),
               updated_at = now() WHERE id = p_doc_id;
    ELSE  -- a failed replacement keeps the previous, still-valid version
        UPDATE public.documents SET last_error = left(p_error, 300), updated_at = now()
         WHERE id = p_doc_id;
    END IF;
    INSERT INTO public.retrieval_audit (request_id, event_type, user_id, tenant_id, roles, status,
        target_type, target_id, change_after)
    VALUES (p_request_id, CASE WHEN p_replace THEN 'document_replaced' ELSE 'document_uploaded' END,
            v_actor, v_tenant, v_roles, 'error', 'document', p_doc_id,
            jsonb_build_object('error', left(p_error, 300)));
    RETURN jsonb_build_object('doc_id', p_doc_id, 'status',
                              CASE WHEN p_replace THEN d.status ELSE 'failed' END);
END $$
""" % {"actor": ACTOR, "manage": MANAGE}

DELETE = """
CREATE OR REPLACE FUNCTION delete_document(p_doc_id text, p_request_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_tenant uuid; v_actor uuid; v_roles text[]; d public.documents%%ROWTYPE; v_n integer;
BEGIN
%(actor)s
%(manage)s
    SELECT count(*) INTO v_n FROM public.chunks WHERE doc_id = p_doc_id;
    DELETE FROM public.documents WHERE id = p_doc_id;  -- chunks cascade (FK ON DELETE CASCADE)
    INSERT INTO public.retrieval_audit (request_id, event_type, user_id, tenant_id, roles, status,
        target_type, target_id, change_before)
    VALUES (p_request_id, 'document_deleted', v_actor, v_tenant, v_roles, 'ok', 'document',
            p_doc_id, jsonb_build_object('bytes', d.size_bytes, 'chunks', v_n,
                                         'classification', d.classification));
    RETURN jsonb_build_object('doc_id', p_doc_id, 'deleted', true, 'chunks_deleted', v_n);
END $$
""" % {"actor": ACTOR, "manage": MANAGE}

META_COLS = """doc_id text, title text, classification text, status text, created_at timestamptz,
    updated_at timestamptz, acl_version integer, chunk_count integer, size_bytes bigint,
    original_filename text, last_error text, uploaded_by_me boolean"""
META_SELECT = """d.id, d.title, d.classification::text, d.status, d.created_at, d.updated_at,
    d.acl_version, d.chunk_count, d.size_bytes, d.original_filename, d.last_error,
    d.uploaded_by IS NOT DISTINCT FROM public.app_user_id()"""

META = f"""
CREATE OR REPLACE FUNCTION document_meta(p_doc_id text) RETURNS TABLE ({META_COLS})
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT {META_SELECT} FROM public.documents d
     WHERE d.id = p_doc_id AND d.tenant_id = public.app_tenant_id()
       AND EXISTS (SELECT 1 FROM public.users u WHERE u.id = public.app_user_id()
                   AND u.tenant_id = d.tenant_id AND u.is_active)
       AND (d.uploaded_by = public.app_user_id()
            OR public.app_can_read(d.tenant_id, d.classification, d.allowed_roles, d.allowed_users))
$$
"""

MY_UPLOADS = f"""
CREATE OR REPLACE FUNCTION my_uploads() RETURNS TABLE ({META_COLS})
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT {META_SELECT} FROM public.documents d
     WHERE d.tenant_id = public.app_tenant_id() AND d.uploaded_by = public.app_user_id()
       AND EXISTS (SELECT 1 FROM public.users u WHERE u.id = public.app_user_id()
                   AND u.tenant_id = d.tenant_id AND u.is_active)
     ORDER BY d.created_at DESC
$$
"""

CHUNKS_POLICY = f"""
    CREATE POLICY chunks_tenant_acl ON chunks FOR SELECT TO {APP_ROLE}
    USING (
        tenant_id = app_tenant_id()
        AND (
            visibility = 'tenant'
            OR (visibility = 'employees' AND app_is_employee())
            OR (visibility = 'restricted'
                AND (allowed_roles && app_roles() OR app_user_id() = ANY (allowed_users)))
        )
        AND EXISTS (SELECT 1 FROM documents d WHERE d.id = chunks.doc_id AND d.status = 'ready')
    )
"""

FUNCS = (
    "register_tenant(text, text, text, text, integer, uuid)",
    "admin_create_user(text, text[], text, integer, uuid)",
    "upload_document_begin(text, text, doc_classification, text[], uuid[], text[], integer, bigint, text, text, uuid)",
    "upload_document_complete(text, jsonb, integer, boolean, bigint, text, text, uuid)",
    "upload_document_fail(text, text, boolean, uuid)",
    "delete_document(text, uuid)",
    "document_meta(text)",
    "my_uploads()",
)
EVENTS = (
    "'retrieval','acl_change','tenant_registered','user_created','document_uploaded',"
    "'document_replaced','document_deleted'"
)


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tenants
            ADD COLUMN source text NOT NULL DEFAULT 'seed' CHECK (source IN ('seed', 'self_service')),
            ADD COLUMN created_at timestamptz NOT NULL DEFAULT now()
        """
    )
    op.execute(
        """
        ALTER TABLE documents
            ADD COLUMN status text NOT NULL DEFAULT 'ready'
                CHECK (status IN ('processing', 'ready', 'failed')),
            ADD COLUMN source text NOT NULL DEFAULT 'seed' CHECK (source IN ('seed', 'upload')),
            ADD COLUMN uploaded_by uuid,
            ADD COLUMN original_filename text CHECK (length(original_filename) <= 120),
            ADD COLUMN content_type text,
            ADD COLUMN size_bytes bigint CHECK (size_bytes IS NULL OR size_bytes >= 0),
            ADD COLUMN chunk_count integer,
            ADD COLUMN last_error text CHECK (length(last_error) <= 300)
        """
    )
    op.execute("CREATE INDEX ix_documents_status ON documents (status) WHERE status <> 'ready'")
    op.execute("ALTER TABLE retrieval_audit DROP CONSTRAINT retrieval_audit_event_type_check")
    op.execute(
        f"ALTER TABLE retrieval_audit ADD CONSTRAINT retrieval_audit_event_type_check "
        f"CHECK (event_type IN ({EVENTS}))"
    )
    op.execute("ALTER TABLE retrieval_audit DROP CONSTRAINT retrieval_audit_target_type_check")
    op.execute(
        "ALTER TABLE retrieval_audit ADD CONSTRAINT retrieval_audit_target_type_check "
        "CHECK (target_type IS NULL OR target_type IN ('document', 'user', 'tenant'))"
    )
    op.execute("DROP POLICY chunks_tenant_acl ON chunks")
    op.execute(CHUNKS_POLICY)
    op.execute(CAN_READ)
    op.execute(
        "REVOKE ALL ON FUNCTION app_can_read(uuid, doc_classification, text[], uuid[]) FROM PUBLIC"
    )
    for sql in (REGISTER, CREATE_USER, BEGIN_UPLOAD, COMPLETE, FAIL, DELETE, META, MY_UPLOADS):
        op.execute(sql)
    for fn in FUNCS:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {APP_ROLE}")


def downgrade() -> None:
    for fn in FUNCS:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")
    op.execute("DROP FUNCTION IF EXISTS app_can_read(uuid, doc_classification, text[], uuid[])")
    op.execute("DROP POLICY chunks_tenant_acl ON chunks")
    op.execute(
        f"""
        CREATE POLICY chunks_tenant_acl ON chunks FOR SELECT TO {APP_ROLE}
        USING (tenant_id = app_tenant_id() AND (visibility = 'tenant'
               OR (visibility = 'employees' AND app_is_employee())
               OR (visibility = 'restricted'
                   AND (allowed_roles && app_roles() OR app_user_id() = ANY (allowed_users)))))
        """
    )
    op.execute("ALTER TABLE retrieval_audit DROP CONSTRAINT retrieval_audit_target_type_check")
    op.execute(
        "ALTER TABLE retrieval_audit ADD CONSTRAINT retrieval_audit_target_type_check "
        "CHECK (target_type IS NULL OR target_type IN ('document', 'user'))"
    )
    op.execute("ALTER TABLE retrieval_audit DROP CONSTRAINT retrieval_audit_event_type_check")
    op.execute(
        "ALTER TABLE retrieval_audit ADD CONSTRAINT retrieval_audit_event_type_check "
        "CHECK (event_type IN ('retrieval', 'acl_change'))"
    )
    op.execute("DROP INDEX IF EXISTS ix_documents_status")
    op.execute(
        """
        ALTER TABLE documents DROP COLUMN last_error, DROP COLUMN chunk_count,
            DROP COLUMN size_bytes, DROP COLUMN content_type, DROP COLUMN original_filename,
            DROP COLUMN uploaded_by, DROP COLUMN source, DROP COLUMN status
        """
    )
    op.execute("ALTER TABLE tenants DROP COLUMN created_at, DROP COLUMN source")
