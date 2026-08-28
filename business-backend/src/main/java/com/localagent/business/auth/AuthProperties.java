package com.localagent.business.auth;

import java.time.Duration;
import java.util.List;
import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties("app.auth")
public record AuthProperties(
    String issuer, String audience, Duration accessTtl, Duration refreshTtl, String keyId,
    String privateKeyPem, String publicKeyPem, boolean allowEphemeralKey,
    List<String> allowedOrigins, boolean secureRefreshCookie,
    int rateLimitAttempts, Duration rateLimitWindow) {}
