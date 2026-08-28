CREATE TABLE email_verification_challenges (
  normalized_email VARCHAR(320) PRIMARY KEY,
  code_hash VARCHAR(64) NOT NULL,
  code_salt VARCHAR(64) NOT NULL,
  expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  resend_available_at TIMESTAMP WITH TIME ZONE NOT NULL,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  delivered_at TIMESTAMP WITH TIME ZONE,
  consumed_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX ix_email_verification_expires_at ON email_verification_challenges(expires_at);
