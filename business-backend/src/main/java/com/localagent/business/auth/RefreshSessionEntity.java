package com.localagent.business.auth;

import com.localagent.business.user.UserEntity;
import jakarta.persistence.*;
import java.time.Instant;
import java.util.UUID;

@Entity @Table(name="refresh_sessions")
public class RefreshSessionEntity {
  @Id private UUID id;
  @ManyToOne(optional=false, fetch=FetchType.LAZY) @JoinColumn(name="user_id") private UserEntity user;
  @Column(name="token_hash",nullable=false,unique=true,length=64) private String tokenHash;
  @Column(name="token_family_id",nullable=false) private UUID tokenFamilyId;
  @Column(name="device_id",nullable=false) private String deviceId;
  @Column(name="expires_at",nullable=false) private Instant expiresAt;
  @Column(name="revoked_at") private Instant revokedAt;
  @Column(name="replaced_by") private UUID replacedBy;
  @Column(name="created_at",nullable=false) private Instant createdAt;
  @Column(name="last_used_at") private Instant lastUsedAt;
  public UUID getId(){return id;} public void setId(UUID v){id=v;}
  public UserEntity getUser(){return user;} public void setUser(UserEntity v){user=v;}
  public String getTokenHash(){return tokenHash;} public void setTokenHash(String v){tokenHash=v;}
  public UUID getTokenFamilyId(){return tokenFamilyId;} public void setTokenFamilyId(UUID v){tokenFamilyId=v;}
  public String getDeviceId(){return deviceId;} public void setDeviceId(String v){deviceId=v;}
  public Instant getExpiresAt(){return expiresAt;} public void setExpiresAt(Instant v){expiresAt=v;}
  public Instant getRevokedAt(){return revokedAt;} public void setRevokedAt(Instant v){revokedAt=v;}
  public UUID getReplacedBy(){return replacedBy;} public void setReplacedBy(UUID v){replacedBy=v;}
  public Instant getCreatedAt(){return createdAt;} public void setCreatedAt(Instant v){createdAt=v;}
  public Instant getLastUsedAt(){return lastUsedAt;} public void setLastUsedAt(Instant v){lastUsedAt=v;}
}
