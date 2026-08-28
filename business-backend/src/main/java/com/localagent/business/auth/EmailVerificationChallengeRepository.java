package com.localagent.business.auth;

import jakarta.persistence.LockModeType;
import java.util.Optional;
import org.springframework.data.jpa.repository.*;
import org.springframework.data.repository.query.Param;

public interface EmailVerificationChallengeRepository extends JpaRepository<EmailVerificationChallengeEntity,String> {
  @Lock(LockModeType.PESSIMISTIC_WRITE)
  @Query("select c from EmailVerificationChallengeEntity c where c.normalizedEmail=:email")
  Optional<EmailVerificationChallengeEntity> findForUpdate(@Param("email") String email);
  @Modifying
  @Query("delete from EmailVerificationChallengeEntity c where c.normalizedEmail=:email and c.codeHash=:hash and c.codeSalt=:salt and c.consumedAt is null")
  int deleteGenerated(@Param("email") String email,@Param("hash") String hash,@Param("salt") String salt);
}
