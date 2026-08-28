package com.localagent.business.user;

import jakarta.persistence.*;
import java.time.Instant;
import java.util.UUID;

@Entity @Table(name="users")
public class UserEntity {
  @Id private UUID id;
  @Column(name="normalized_email", nullable=false, unique=true) private String normalizedEmail;
  @Column(name="password_hash", nullable=false) private String passwordHash;
  @Column(name="display_name", nullable=false) private String displayName;
  @Column(nullable=false) private String status;
  @Column(name="email_verified", nullable=false) private boolean emailVerified;
  @Column(name="created_at", nullable=false) private Instant createdAt;
  @Column(name="updated_at", nullable=false) private Instant updatedAt;
  public UUID getId(){return id;} public void setId(UUID v){id=v;}
  public String getNormalizedEmail(){return normalizedEmail;} public void setNormalizedEmail(String v){normalizedEmail=v;}
  public String getPasswordHash(){return passwordHash;} public void setPasswordHash(String v){passwordHash=v;}
  public String getDisplayName(){return displayName;} public void setDisplayName(String v){displayName=v;}
  public String getStatus(){return status;} public void setStatus(String v){status=v;}
  public boolean isEmailVerified(){return emailVerified;} public void setEmailVerified(boolean v){emailVerified=v;}
  public Instant getCreatedAt(){return createdAt;} public void setCreatedAt(Instant v){createdAt=v;}
  public Instant getUpdatedAt(){return updatedAt;} public void setUpdatedAt(Instant v){updatedAt=v;}
}
