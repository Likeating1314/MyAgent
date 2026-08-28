CREATE TABLE users (
  id UUID PRIMARY KEY,
  normalized_email VARCHAR(320) NOT NULL UNIQUE,
  password_hash VARCHAR(100) NOT NULL,
  display_name VARCHAR(80) NOT NULL,
  status VARCHAR(32) NOT NULL,
  email_verified BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE TABLE refresh_sessions (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id),
  token_hash VARCHAR(64) NOT NULL UNIQUE,
  token_family_id UUID NOT NULL,
  device_id VARCHAR(128) NOT NULL,
  expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  revoked_at TIMESTAMP WITH TIME ZONE,
  replaced_by UUID REFERENCES refresh_sessions(id),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  last_used_at TIMESTAMP WITH TIME ZONE
);
CREATE INDEX ix_refresh_sessions_family ON refresh_sessions(token_family_id);
CREATE INDEX ix_refresh_sessions_user ON refresh_sessions(user_id);
