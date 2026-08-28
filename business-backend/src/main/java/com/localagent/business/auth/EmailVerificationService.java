package com.localagent.business.auth;

import com.localagent.business.auth.AuthDtos.EmailCodeResponse;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.time.Instant;
import java.util.HexFormat;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

@Service
public class EmailVerificationService {
  private final EmailVerificationChallengeRepository challenges; private final VerificationCodeGenerator generator; private final VerificationEmailSender sender; private final EmailVerificationProperties properties; private final SecureRandom random=new SecureRandom(); private final TransactionTemplate transactions;
  public EmailVerificationService(EmailVerificationChallengeRepository challenges,VerificationCodeGenerator generator,VerificationEmailSender sender,EmailVerificationProperties properties,PlatformTransactionManager manager){this.challenges=challenges;this.generator=generator;this.sender=sender;this.properties=properties;this.transactions=new TransactionTemplate(manager);}
  public EmailCodeResponse send(String email){long expires=properties.ttl().toSeconds(),cooldown=properties.resendCooldown().toSeconds();String code=generator.generate();byte[] saltBytes=new byte[32];random.nextBytes(saltBytes);String salt=HexFormat.of().formatHex(saltBytes),hash=hash(email,code,salt);try{transactions.executeWithoutResult(status->{Instant now=Instant.now();EmailVerificationChallengeEntity c=challenges.findForUpdate(email).orElseGet(EmailVerificationChallengeEntity::new);if(c.getNormalizedEmail()!=null&&c.getResendAvailableAt().isAfter(now))throw cooldown();c.setNormalizedEmail(email);c.setCodeHash(hash);c.setCodeSalt(salt);c.setExpiresAt(now.plus(properties.ttl()));c.setResendAvailableAt(now.plus(properties.resendCooldown()));c.setFailedAttempts(0);c.setDeliveredAt(null);c.setConsumedAt(null);if(c.getCreatedAt()==null)c.setCreatedAt(now);c.setUpdatedAt(now);challenges.saveAndFlush(c);});}catch(DataIntegrityViolationException race){throw cooldown();}try{sender.send(email,code,expiryMinutes());transactions.executeWithoutResult(status->{EmailVerificationChallengeEntity c=challenges.findForUpdate(email).orElse(null);if(c!=null&&hash.equals(c.getCodeHash())&&salt.equals(c.getCodeSalt())){Instant now=Instant.now();c.setDeliveredAt(now);c.setUpdatedAt(now);challenges.save(c);}});}catch(RuntimeException failure){transactions.executeWithoutResult(status->challenges.deleteGenerated(email,hash,salt));throw new AuthException("email_delivery_failed","验证码邮件暂时无法发送，请稍后重试。",HttpStatus.SERVICE_UNAVAILABLE);}return new EmailCodeResponse(expires,cooldown);}
  public void verifyAndConsume(String email,String code){EmailVerificationChallengeEntity c=challenges.findForUpdate(email).orElseThrow(InvalidVerificationCodeException::new);Instant now=Instant.now();boolean eligible=c.getDeliveredAt()!=null&&c.getConsumedAt()==null&&c.getExpiresAt().isAfter(now)&&c.getFailedAttempts()<properties.maxAttempts();boolean matches=eligible&&MessageDigest.isEqual(HexFormat.of().parseHex(c.getCodeHash()),HexFormat.of().parseHex(hash(email,code,c.getCodeSalt())));if(!matches){if(c.getDeliveredAt()!=null&&c.getConsumedAt()==null&&c.getFailedAttempts()<properties.maxAttempts()){c.setFailedAttempts(c.getFailedAttempts()+1);c.setUpdatedAt(now);challenges.save(c);}throw new InvalidVerificationCodeException();}c.setConsumedAt(now);c.setUpdatedAt(now);challenges.save(c);}
  private AuthException cooldown(){return new AuthException("verification_code_cooldown","验证码发送过于频繁，请稍后重试。",HttpStatus.TOO_MANY_REQUESTS);}
  private long expiryMinutes(){long seconds=properties.ttl().getSeconds()+(properties.ttl().getNano()>0?1:0);return Math.max(1,Math.floorDiv(seconds+59,60));}
  private String hash(String email,String code,String salt){try{Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(properties.codeSecret().getBytes(StandardCharsets.UTF_8),"HmacSHA256"));return HexFormat.of().formatHex(mac.doFinal((email+"\n"+salt+"\n"+code).getBytes(StandardCharsets.UTF_8)));}catch(GeneralSecurityException e){throw new IllegalStateException("Unable to protect verification code",e);}}
}
