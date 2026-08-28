package com.localagent.business.auth;
import java.security.SecureRandom;
import org.springframework.stereotype.Component;
@Component public class SecureVerificationCodeGenerator implements VerificationCodeGenerator {
  private final SecureRandom random=new SecureRandom();
  @Override public String generate(){return String.format("%06d",random.nextInt(1_000_000));}
}
