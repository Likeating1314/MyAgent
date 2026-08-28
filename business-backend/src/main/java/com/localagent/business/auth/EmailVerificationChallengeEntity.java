package com.localagent.business.auth;

import jakarta.persistence.*;
import java.time.Instant;

@Entity @Table(name="email_verification_challenges")
public class EmailVerificationChallengeEntity {
  @Id @Column(name="normalized_email",length=320) private String normalizedEmail;
  @Column(name="code_hash",nullable=false,length=64) private String codeHash;
  @Column(name="code_salt",nullable=false,length=64) private String codeSalt;
  @Column(name="expires_at",nullable=false) private Instant expiresAt;
  @Column(name="resend_available_at",nullable=false) private Instant resendAvailableAt;
  @Column(name="failed_attempts",nullable=false) private int failedAttempts;
  @Column(name="delivered_at") private Instant deliveredAt;
  @Column(name="consumed_at") private Instant consumedAt;
  @Column(name="created_at",nullable=false) private Instant createdAt;
  @Column(name="updated_at",nullable=false) private Instant updatedAt;
  public String getNormalizedEmail(){return normalizedEmail;} public void setNormalizedEmail(String v){normalizedEmail=v;}
  public String getCodeHash(){return codeHash;} public void setCodeHash(String v){codeHash=v;}
  public String getCodeSalt(){return codeSalt;} public void setCodeSalt(String v){codeSalt=v;}
  public Instant getExpiresAt(){return expiresAt;} public void setExpiresAt(Instant v){expiresAt=v;}
  public Instant getResendAvailableAt(){return resendAvailableAt;} public void setResendAvailableAt(Instant v){resendAvailableAt=v;}
  public int getFailedAttempts(){return failedAttempts;} public void setFailedAttempts(int v){failedAttempts=v;}
  public Instant getDeliveredAt(){return deliveredAt;} public void setDeliveredAt(Instant v){deliveredAt=v;}
  public Instant getConsumedAt(){return consumedAt;} public void setConsumedAt(Instant v){consumedAt=v;}
  public Instant getCreatedAt(){return createdAt;} public void setCreatedAt(Instant v){createdAt=v;}
  public Instant getUpdatedAt(){return updatedAt;} public void setUpdatedAt(Instant v){updatedAt=v;}
}
