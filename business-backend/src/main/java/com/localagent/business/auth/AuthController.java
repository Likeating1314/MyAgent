package com.localagent.business.auth;

import com.localagent.business.auth.AuthDtos.*; import com.localagent.business.user.UserRepository;
import jakarta.servlet.http.*; import jakarta.validation.Valid; import java.time.Duration; import java.util.Map; import java.util.UUID;
import org.springframework.http.*; import org.springframework.security.oauth2.jwt.Jwt; import org.springframework.security.core.annotation.AuthenticationPrincipal; import org.springframework.web.bind.annotation.*;

@RestController
public class AuthController {
  private static final String COOKIE="refresh_token"; private final AuthService auth; private final EmailVerificationService verification; private final UserRepository users; private final RateLimiter limiter; private final AuthProperties p;
  public AuthController(AuthService a,EmailVerificationService verification,UserRepository u,RateLimiter l,AuthProperties p){auth=a;this.verification=verification;users=u;limiter=l;this.p=p;}
  @PostMapping("/api/v1/auth/register/email-code") ResponseEntity<EmailCodeResponse> sendRegistrationCode(@Valid @RequestBody EmailCodeRequest r,HttpServletRequest req){String email=AuthService.normalize(r.email());limiter.check("register-email-code:ip:"+req.getRemoteAddr());limiter.check("register-email-code:email:"+email);return ResponseEntity.accepted().body(verification.send(email));}
  @PostMapping("/api/v1/auth/register") ResponseEntity<AuthResponse> register(@Valid @RequestBody RegisterRequest r,HttpServletRequest req,@RequestHeader(name="X-Auth-Client",required=false)String client){limit(req,r.email(),"register");return withCookie(auth.register(r),client,req);}
  @PostMapping("/api/v1/auth/login") ResponseEntity<AuthResponse> login(@Valid @RequestBody LoginRequest r,HttpServletRequest req,@RequestHeader(name="X-Auth-Client",required=false)String client){limit(req,r.email(),"login");return withCookie(auth.login(r),client,req);}
  @PostMapping("/api/v1/auth/refresh") ResponseEntity<AuthResponse> refresh(@Valid @RequestBody RefreshRequest r,@CookieValue(name=COOKIE,required=false)String cookie,@RequestHeader(name="X-Auth-Client",required=false)String client,HttpServletRequest req){return withCookie(auth.refresh(first(r.refreshToken(),cookie),r.deviceId()),client,req);}
  @PostMapping("/api/v1/auth/logout") ResponseEntity<Void> logout(@RequestBody(required=false) LogoutRequest r,@CookieValue(name=COOKIE,required=false)String cookie){auth.logout(first(r==null?null:r.refreshToken(),cookie));return ResponseEntity.noContent().header(HttpHeaders.SET_COOKIE,clearCookie().toString()).build();}
  @GetMapping("/api/v1/users/me") UserProfile me(@AuthenticationPrincipal Jwt jwt){return users.findById(UUID.fromString(jwt.getSubject())).map(auth::profile).orElseThrow(()->new AuthException("user_not_found","用户不存在。",HttpStatus.NOT_FOUND));}
  private void limit(HttpServletRequest req,String email,String action){limiter.check(action+":"+req.getRemoteAddr()+":"+AuthService.normalize(email));}
  private ResponseEntity<AuthResponse> withCookie(AuthResponse a,String client,HttpServletRequest req){boolean trustedElectron="electron".equals(client)&&req.getHeader("Origin")==null&&req.getHeader("Sec-Fetch-Site")==null&&req.getHeader("Sec-Fetch-Mode")==null;AuthResponse safe=new AuthResponse(a.accessToken(),trustedElectron?a.refreshToken():null,a.expiresAt(),a.user());return ResponseEntity.ok().header(HttpHeaders.SET_COOKIE,cookie(a.refreshToken()).toString()).body(safe);}
  private ResponseCookie cookie(String value){return ResponseCookie.from(COOKIE,value).httpOnly(true).secure(p.secureRefreshCookie()).sameSite("Strict").path("/api/v1/auth").maxAge(p.refreshTtl()).build();}
  private ResponseCookie clearCookie(){return ResponseCookie.from(COOKIE,"").httpOnly(true).secure(p.secureRefreshCookie()).sameSite("Strict").path("/api/v1/auth").maxAge(Duration.ZERO).build();}
  private String first(String body,String cookie){return body!=null&&!body.isBlank()?body:cookie;}
}
