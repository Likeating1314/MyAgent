package com.localagent.business.auth;

import jakarta.validation.constraints.*; import java.time.Instant; import java.util.List; import java.util.UUID;

public final class AuthDtos {
  private AuthDtos(){}
  public record RegisterRequest(@NotBlank @Email @Size(max=320) String email,@NotBlank @Pattern(regexp="\\d{6}") String verificationCode,@NotBlank @Size(min=10,max=128) String password,@NotBlank @Size(max=80) String displayName,@NotBlank @Size(max=128) String deviceId){}
  public record EmailCodeRequest(@NotBlank @Email @Size(max=320) String email){}
  public record EmailCodeResponse(long expiresInSeconds,long resendAfterSeconds){}
  public record LoginRequest(@NotBlank @Email @Size(max=320) String email,@NotBlank @Size(max=128) String password,@NotBlank @Size(max=128) String deviceId){}
  public record RefreshRequest(@Size(max=2048) String refreshToken,@NotBlank @Size(max=128) String deviceId){}
  public record LogoutRequest(@Size(max=2048) String refreshToken){}
  public record UserProfile(UUID id,String email,String displayName,String status,boolean emailVerified,List<String> roles){}
  public record AuthResponse(String accessToken,String refreshToken,Instant expiresAt,UserProfile user){}
}
