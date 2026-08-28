package com.localagent.business;

import com.fasterxml.jackson.databind.*;
import com.localagent.business.auth.*;
import com.localagent.business.user.*;
import com.nimbusds.jwt.SignedJWT;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.context.annotation.*;
import org.springframework.http.*;
import org.springframework.mail.javamail.*;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.annotation.DirtiesContext;

@SpringBootTest(webEnvironment=SpringBootTest.WebEnvironment.RANDOM_PORT)
@DirtiesContext(classMode=DirtiesContext.ClassMode.BEFORE_EACH_TEST_METHOD)
@Import(AuthIntegrationTest.EmailTestConfiguration.class)
class AuthIntegrationTest {
  static final String CODE="123456";
  @Autowired TestRestTemplate http; @Autowired UserRepository users; @Autowired RefreshSessionRepository refreshes; @Autowired EmailVerificationChallengeRepository challenges; @Autowired ObjectMapper json; @Autowired CapturingEmailSender mail; @Autowired PasswordEncoder passwords;
  @BeforeEach void clearData(){refreshes.deleteAll();challenges.deleteAll();users.deleteAll();mail.sent.clear();mail.failNext=false;mail.generator.set(123456);}
  Map<String,Object> registration(String email){return registration(email,CODE);}
  Map<String,Object> registration(String email,String code){return Map.of("email",email,"verificationCode",code,"password","correct-horse-battery","displayName","测试用户","deviceId","device-a");}
  ResponseEntity<String> post(String path,Object body){return http.postForEntity(path,body,String.class);}
  ResponseEntity<String> send(String email){return post("/api/v1/auth/register/email-code",Map.of("email",email));}
  void sendOk(String email){Assertions.assertEquals(202,send(email).getStatusCode().value());}
  String errorCode(ResponseEntity<String> response) throws Exception{return json.readTree(response.getBody()).get("code").asText();}
  String cookie(ResponseEntity<?> r){return Objects.requireNonNull(r.getHeaders().getFirst(HttpHeaders.SET_COOKIE)).split(";",2)[0];}
  String tokenHash(String cookie) throws Exception{String raw=cookie.substring(cookie.indexOf('=')+1);return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw.getBytes(StandardCharsets.UTF_8)));}

  @Test void registrationRequiresSentStrictMatchingCodeAndDoesNotCreateCredentialsOnFailure() throws Exception {
    ResponseEntity<String> unsent=post("/api/v1/auth/register",registration("unsent@example.com"));
    Assertions.assertEquals(422,unsent.getStatusCode().value());Assertions.assertEquals("invalid_verification_code",errorCode(unsent));
    Map<String,Object> missing=new HashMap<>(registration("missing@example.com"));missing.remove("verificationCode");
    Assertions.assertEquals("invalid_verification_code",errorCode(post("/api/v1/auth/register",missing)));
    sendOk("owner@example.com");
    Assertions.assertEquals(422,post("/api/v1/auth/register",registration("owner@example.com","000000")).getStatusCode().value());
    Assertions.assertEquals(422,post("/api/v1/auth/register",registration("other@example.com",CODE)).getStatusCode().value());
    Assertions.assertEquals(0,users.count());Assertions.assertEquals(0,refreshes.count());Assertions.assertNull(unsent.getHeaders().getFirst(HttpHeaders.SET_COOKIE));
  }

  @Test void expiredAndExhaustedCodesReturnOneGenericErrorAndPersistAttempts() throws Exception {
    sendOk("expired@example.com");var expired=challenges.findById("expired@example.com").orElseThrow();expired.setExpiresAt(Instant.now().minusSeconds(1));challenges.saveAndFlush(expired);
    ResponseEntity<String> expiredResponse=post("/api/v1/auth/register",registration("expired@example.com"));
    Assertions.assertEquals(422,expiredResponse.getStatusCode().value());Assertions.assertEquals("invalid_verification_code",errorCode(expiredResponse));
    sendOk("attempts@example.com");for(int i=0;i<5;i++)Assertions.assertEquals(422,post("/api/v1/auth/register",registration("attempts@example.com","000000")).getStatusCode().value());
    Assertions.assertEquals(5,challenges.findById("attempts@example.com").orElseThrow().getFailedAttempts());Assertions.assertEquals(422,post("/api/v1/auth/register",registration("attempts@example.com",CODE)).getStatusCode().value());Assertions.assertEquals(0,users.count());Assertions.assertEquals(0,refreshes.count());
  }

  @Test void correctCodeCreatesVerifiedUserAndIssuesBrowserCredentials() throws Exception {
    sendOk("User@Example.com");ResponseEntity<String> response=post("/api/v1/auth/register",registration("user@example.com"));Assertions.assertEquals(200,response.getStatusCode().value());var user=users.findByNormalizedEmail("user@example.com").orElseThrow();Assertions.assertTrue(user.isEmailVerified());Assertions.assertNotEquals("correct-horse-battery",user.getPasswordHash());Assertions.assertTrue(user.getPasswordHash().startsWith("$2"));Assertions.assertEquals(1,refreshes.count());Assertions.assertNotNull(json.readTree(response.getBody()).get("accessToken").asText());Assertions.assertTrue(json.readTree(response.getBody()).get("refreshToken").isNull());Assertions.assertTrue(cookie(response).startsWith("refresh_token="));Assertions.assertNotNull(challenges.findById("user@example.com").orElseThrow().getConsumedAt());
  }

  @Test void codeIsSingleUseAndConcurrentRegistrationAllowsAtMostOneSuccess() throws Exception {
    sendOk("once@example.com");Assertions.assertEquals(200,post("/api/v1/auth/register",registration("once@example.com")).getStatusCode().value());Assertions.assertEquals(409,post("/api/v1/auth/register",registration("once@example.com")).getStatusCode().value());
    sendOk("race@example.com");ExecutorService pool=Executors.newFixedThreadPool(2);CountDownLatch start=new CountDownLatch(1);Callable<Integer> call=()->{start.await();return post("/api/v1/auth/register",registration("race@example.com")).getStatusCode().value();};Future<Integer> a=pool.submit(call),b=pool.submit(call);start.countDown();List<Integer> statuses=List.of(a.get(10,TimeUnit.SECONDS),b.get(10,TimeUnit.SECONDS));pool.shutdownNow();Assertions.assertEquals(1,statuses.stream().filter(s->s==200).count());Assertions.assertEquals(1,users.findAll().stream().filter(u->u.getNormalizedEmail().equals("race@example.com")).count());
  }

  @Test void resendCooldownAndNewCodeInvalidationAreServerEnforced() throws Exception {
    mail.generator.set(111111);sendOk("resend@example.com");Assertions.assertEquals(429,send("resend@example.com").getStatusCode().value());var old=challenges.findById("resend@example.com").orElseThrow();old.setResendAvailableAt(Instant.now().minusSeconds(1));challenges.saveAndFlush(old);mail.generator.set(222222);sendOk("resend@example.com");Assertions.assertEquals(422,post("/api/v1/auth/register",registration("resend@example.com","111111")).getStatusCode().value());Assertions.assertEquals(200,post("/api/v1/auth/register",registration("resend@example.com","222222")).getStatusCode().value());
  }

  @Test void registeredEmailIsGenericAndDeliveryFailureAllowsImmediateRetry() throws Exception {
    createHistoricalUser("registered@example.com",true);int before=mail.sent.size();ResponseEntity<String> known=send("Registered@Example.com"),unknown=send("unknown@example.com");Assertions.assertEquals(202,known.getStatusCode().value());Assertions.assertEquals(202,unknown.getStatusCode().value());Assertions.assertEquals(601,json.readTree(known.getBody()).get("expiresInSeconds").asInt());Assertions.assertEquals(json.readTree(known.getBody()),json.readTree(unknown.getBody()));Assertions.assertEquals(before+2,mail.sent.size());Assertions.assertTrue(mail.sent.stream().anyMatch(value->value.equals("registered@example.com:"+CODE+":11")));Assertions.assertTrue(mail.sent.stream().anyMatch(value->value.equals("unknown@example.com:"+CODE+":11")));Assertions.assertEquals(429,send("registered@example.com").getStatusCode().value());Assertions.assertEquals(429,send("unknown@example.com").getStatusCode().value());mail.failNext=true;ResponseEntity<String> failed=send("failure@example.com");Assertions.assertEquals(503,failed.getStatusCode().value());Assertions.assertEquals("email_delivery_failed",errorCode(failed));Assertions.assertTrue(challenges.findById("failure@example.com").isEmpty());Assertions.assertEquals(202,send("failure@example.com").getStatusCode().value());
  }

  @Test void smtpFailureIsIndistinguishableForRegisteredAndUnregisteredEmailsAndAllowsRetry() throws Exception {
    createHistoricalUser("known-failure@example.com",true);mail.failNext=true;ResponseEntity<String> known=send("known-failure@example.com");Assertions.assertEquals(503,known.getStatusCode().value());Assertions.assertEquals("email_delivery_failed",errorCode(known));Assertions.assertTrue(challenges.findById("known-failure@example.com").isEmpty());mail.failNext=true;ResponseEntity<String> unknown=send("unknown-failure@example.com");Assertions.assertEquals(503,unknown.getStatusCode().value());Assertions.assertEquals("email_delivery_failed",errorCode(unknown));Assertions.assertTrue(challenges.findById("unknown-failure@example.com").isEmpty());Assertions.assertEquals(202,send("known-failure@example.com").getStatusCode().value());Assertions.assertEquals(202,send("unknown-failure@example.com").getStatusCode().value());
  }

  @Test void challengeStoresOnlySaltedHmacAndSeparateRateLimitBucketsWork() {
    sendOk("hash@example.com");var challenge=challenges.findById("hash@example.com").orElseThrow();Assertions.assertNotEquals(CODE,challenge.getCodeHash());Assertions.assertEquals(64,challenge.getCodeHash().length());Assertions.assertEquals(64,challenge.getCodeSalt().length());AuthProperties props=new AuthProperties("i","a",java.time.Duration.ofMinutes(1),java.time.Duration.ofDays(1),"k","","",true,List.of(),false,2,java.time.Duration.ofMinutes(1));RateLimiter limiter=new RateLimiter(props);limiter.check("ip:one");limiter.check("email:one");limiter.check("ip:one");limiter.check("email:one");Assertions.assertThrows(AuthException.class,()->limiter.check("ip:one"));Assertions.assertThrows(AuthException.class,()->limiter.check("email:one"));Assertions.assertDoesNotThrow(()->limiter.check("ip:two"));Assertions.assertDoesNotThrow(()->limiter.check("email:two"));
  }

  @Test void legacyUnverifiedUsersCanStillLogin(){createHistoricalUser("legacy@example.com",false);Assertions.assertEquals(200,post("/api/v1/auth/login",Map.of("email","legacy@example.com","password","correct-horse-battery","deviceId","legacy-device")).getStatusCode().value());}

  @Test void commaSeparatedDevelopmentOriginsPassRegistrationPreflight(){
    for(String origin:List.of("http://127.0.0.1:5173","http://localhost:5173")){
      HttpHeaders headers=new HttpHeaders();headers.setOrigin(origin);headers.setAccessControlRequestMethod(HttpMethod.POST);headers.setAccessControlRequestHeaders(List.of(HttpHeaders.CONTENT_TYPE));
      ResponseEntity<String> response=http.exchange("/api/v1/auth/register/email-code",HttpMethod.OPTIONS,new HttpEntity<>(headers),String.class);
      Assertions.assertEquals(200,response.getStatusCode().value());Assertions.assertEquals(origin,response.getHeaders().getAccessControlAllowOrigin());Assertions.assertTrue(response.getHeaders().getAccessControlAllowMethods().contains(HttpMethod.POST));
    }
  }

  @Test void accessClaimsJwksRefreshRotationLogoutAndElectronBoundaryRemainIntact() throws Exception {
    sendOk("claims@example.com");ResponseEntity<String> registered=post("/api/v1/auth/register",registration("claims@example.com"));String token=json.readTree(registered.getBody()).get("accessToken").asText();var jwt=SignedJWT.parse(token);Assertions.assertEquals("test-key",jwt.getHeader().getKeyID());Assertions.assertEquals("http://issuer.test",jwt.getJWTClaimsSet().getIssuer());Assertions.assertEquals(List.of("USER"),jwt.getJWTClaimsSet().getStringListClaim("roles"));Assertions.assertFalse(http.getForObject("/.well-known/jwks.json",String.class).contains("\"d\""));String first=cookie(registered);HttpHeaders headers=new HttpHeaders();headers.add(HttpHeaders.COOKIE,first);Assertions.assertEquals(200,http.exchange("/api/v1/auth/refresh",HttpMethod.POST,new HttpEntity<>(Map.of("deviceId","device-a"),headers),String.class).getStatusCode().value());Assertions.assertEquals(401,http.exchange("/api/v1/auth/refresh",HttpMethod.POST,new HttpEntity<>(Map.of("deviceId","device-a"),headers),String.class).getStatusCode().value());HttpHeaders electronHeaders=new HttpHeaders();electronHeaders.set("X-Auth-Client","electron");sendOk("electron@example.com");ResponseEntity<String> electron=http.exchange("/api/v1/auth/register",HttpMethod.POST,new HttpEntity<>(registration("electron@example.com"),electronHeaders),String.class);Assertions.assertFalse(json.readTree(electron.getBody()).get("refreshToken").isNull());HttpHeaders browserSpoof=new HttpHeaders();browserSpoof.set("X-Auth-Client","electron");browserSpoof.setOrigin("http://127.0.0.1:5173");sendOk("browser@example.com");ResponseEntity<String> browser=http.exchange("/api/v1/auth/register",HttpMethod.POST,new HttpEntity<>(registration("browser@example.com"),browserSpoof),String.class);Assertions.assertTrue(json.readTree(browser.getBody()).get("refreshToken").isNull());
  }

  private void createHistoricalUser(String email,boolean verified){Instant now=Instant.now();UserEntity user=new UserEntity();user.setId(UUID.randomUUID());user.setNormalizedEmail(AuthService.normalize(email));user.setPasswordHash(passwords.encode("correct-horse-battery"));user.setDisplayName("历史用户");user.setStatus("ACTIVE");user.setEmailVerified(verified);user.setCreatedAt(now);user.setUpdatedAt(now);users.saveAndFlush(user);}

  @TestConfiguration static class EmailTestConfiguration {
    @Bean JavaMailSender javaMailSender(){return new JavaMailSenderImpl();}
    @Bean @Primary CapturingEmailSender capturingEmailSender(){return new CapturingEmailSender();}
    @Bean @Primary VerificationCodeGenerator deterministicVerificationCodeGenerator(CapturingEmailSender sender){return ()->String.format("%06d",sender.generator.get());}
  }
  static class CapturingEmailSender implements VerificationEmailSender {
    final AtomicInteger generator=new AtomicInteger(123456);final List<String> sent=new CopyOnWriteArrayList<>();volatile boolean failNext;
    @Override public void send(String email,String code,long minutes){if(failNext){failNext=false;throw new IllegalStateException("simulated delivery failure");}sent.add(email+":"+code+":"+minutes);}
  }
}
