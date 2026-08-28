package com.localagent.business.auth;
import java.time.Instant; import java.util.Optional; import java.util.UUID;
import jakarta.persistence.LockModeType; import org.springframework.data.jpa.repository.*; import org.springframework.data.repository.query.Param;
public interface RefreshSessionRepository extends JpaRepository<RefreshSessionEntity, UUID> {
  Optional<RefreshSessionEntity> findByTokenHash(String hash);
  @Lock(LockModeType.PESSIMISTIC_WRITE)
  @Query("select r from RefreshSessionEntity r where r.tokenHash=:hash")
  Optional<RefreshSessionEntity> findByTokenHashForUpdate(@Param("hash") String hash);
  @Modifying @Query("update RefreshSessionEntity r set r.revokedAt=:now where r.tokenFamilyId=:family and r.revokedAt is null")
  int revokeFamily(@Param("family") UUID family, @Param("now") Instant now);
}
