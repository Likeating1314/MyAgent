package com.localagent.business.user;
import java.util.Optional; import java.util.UUID; import org.springframework.data.jpa.repository.JpaRepository;
public interface UserRepository extends JpaRepository<UserEntity, UUID> { Optional<UserEntity> findByNormalizedEmail(String email); }
