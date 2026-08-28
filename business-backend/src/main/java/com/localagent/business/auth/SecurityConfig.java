package com.localagent.business.auth;

import java.util.List;
import java.util.Objects;
import java.util.stream.Stream;
import org.springframework.context.annotation.*; import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder; import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.oauth2.jwt.*; import org.springframework.security.oauth2.core.*;
import org.springframework.security.oauth2.core.DelegatingOAuth2TokenValidator;
import org.springframework.security.web.SecurityFilterChain; import org.springframework.web.cors.*;

@Configuration
public class SecurityConfig {
  @Bean PasswordEncoder passwordEncoder(){ return new BCryptPasswordEncoder(12); }
  @Bean JwtDecoder jwtDecoder(RsaKeyMaterial keys, AuthProperties p){
    NimbusJwtDecoder decoder=NimbusJwtDecoder.withPublicKey(keys.publicKey()).build();
    OAuth2TokenValidator<Jwt> issuer=JwtValidators.createDefaultWithIssuer(p.issuer());
    OAuth2TokenValidator<Jwt> audience=jwt -> jwt.getAudience().contains(p.audience()) ? OAuth2TokenValidatorResult.success() : OAuth2TokenValidatorResult.failure(new OAuth2Error("invalid_token","Invalid audience",null));
    decoder.setJwtValidator(new DelegatingOAuth2TokenValidator<>(issuer,audience)); return decoder;
  }
  @Bean SecurityFilterChain chain(HttpSecurity http) throws Exception {
    return http.csrf(csrf->csrf.disable()).cors(c->{}).authorizeHttpRequests(a->a
      .requestMatchers("/api/v1/auth/**","/.well-known/jwks.json","/error").permitAll()
      .anyRequest().authenticated()).oauth2ResourceServer(o->o.jwt(j->{})).build();
  }
  @Bean CorsConfigurationSource corsConfigurationSource(AuthProperties p){
    CorsConfiguration c=new CorsConfiguration();
    List<String> origins=Stream.ofNullable(p.allowedOrigins()).flatMap(List::stream).filter(Objects::nonNull).flatMap(value->Stream.of(value.split(","))).map(String::trim).filter(value->!value.isEmpty()).distinct().toList();
    c.setAllowedOrigins(origins); c.setAllowedMethods(List.of("GET","POST","OPTIONS")); c.setAllowedHeaders(List.of("Authorization","Content-Type","X-Device-Id")); c.setAllowCredentials(true); UrlBasedCorsConfigurationSource s=new UrlBasedCorsConfigurationSource(); s.registerCorsConfiguration("/**",c); return s;
  }
}
