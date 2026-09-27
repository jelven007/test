CREATE TABLE IF NOT EXISTS banxia.app_user (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    email_verified_at TIMESTAMPTZ NOT NULL,
    failed_login_count SMALLINT NOT NULL DEFAULT 0
        CHECK (failed_login_count >= 0),
    locked_until TIMESTAMPTZ,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (email = lower(email)),
    CHECK (char_length(email) BETWEEN 3 AND 254)
);

CREATE UNIQUE INDEX IF NOT EXISTS app_user_email_unique
    ON banxia.app_user (lower(email));

CREATE TABLE IF NOT EXISTS banxia.email_verification_challenge (
    challenge_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'register'
        CHECK (purpose IN ('register')),
    code_digest CHAR(64) NOT NULL
        CHECK (code_digest ~ '^[0-9a-f]{64}$'),
    requested_ip TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    attempt_count SMALLINT NOT NULL DEFAULT 0
        CHECK (attempt_count BETWEEN 0 AND 5),
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (email = lower(email)),
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS email_challenge_email_created
    ON banxia.email_verification_challenge (email, created_at DESC);

CREATE INDEX IF NOT EXISTS email_challenge_ip_created
    ON banxia.email_verification_challenge (requested_ip, created_at DESC);

CREATE TABLE IF NOT EXISTS banxia.user_session (
    token_digest CHAR(64) PRIMARY KEY
        CHECK (token_digest ~ '^[0-9a-f]{64}$'),
    user_id UUID NOT NULL
        REFERENCES banxia.app_user(user_id) ON DELETE CASCADE,
    csrf_digest CHAR(64) NOT NULL
        CHECK (csrf_digest ~ '^[0-9a-f]{64}$'),
    user_agent TEXT NOT NULL DEFAULT '',
    remote_ip TEXT NOT NULL DEFAULT '',
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS user_session_active_user
    ON banxia.user_session (user_id, expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS banxia.new_board_reference (
    new_board_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by UUID NOT NULL
        REFERENCES banxia.app_user(user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (char_length(name) BETWEEN 1 AND 40),
    CHECK (char_length(description) <= 200),
    CHECK (sort_order BETWEEN 0 AND 10000)
);

CREATE UNIQUE INDEX IF NOT EXISTS new_board_reference_name_unique
    ON banxia.new_board_reference (lower(name));

ALTER TABLE banxia.security_master
    ADD COLUMN IF NOT EXISTS new_board_id UUID
        REFERENCES banxia.new_board_reference(new_board_id)
        ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS security_master_new_board
    ON banxia.security_master (new_board_id)
    WHERE new_board_id IS NOT NULL;
