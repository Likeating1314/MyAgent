package com.localagent.business.auth;
import org.springframework.http.HttpStatus;
public class InvalidVerificationCodeException extends AuthException {
  public InvalidVerificationCodeException(){super("invalid_verification_code","邮箱验证码无效或已过期。",HttpStatus.UNPROCESSABLE_ENTITY);}
}
