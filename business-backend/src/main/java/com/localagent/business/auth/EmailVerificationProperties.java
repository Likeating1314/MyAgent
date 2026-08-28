package com.localagent.business.auth;

import jakarta.validation.constraints.*;
import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

@Validated
@ConfigurationProperties("app.email-verification")
public record EmailVerificationProperties(
    @NotBlank @Size(min=32) String codeSecret,
    @NotNull Duration ttl,
    @NotNull Duration resendCooldown,
    @Min(1) int maxAttempts,
    @NotBlank String mailFrom) {
  public EmailVerificationProperties {
    if (ttl != null && (ttl.isZero() || ttl.isNegative())) throw new IllegalArgumentException("Email verification TTL must be positive");
    if (resendCooldown != null && (resendCooldown.isZero() || resendCooldown.isNegative())) throw new IllegalArgumentException("Email verification cooldown must be positive");
  }
}
