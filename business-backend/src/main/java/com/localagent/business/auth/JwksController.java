package com.localagent.business.auth;
import com.nimbusds.jose.jwk.RSAKey; import java.util.Map; import org.springframework.web.bind.annotation.*;
@RestController public class JwksController { private final RsaKeyMaterial k; public JwksController(RsaKeyMaterial k){this.k=k;} @GetMapping("/.well-known/jwks.json") Map<String,Object> jwks(){return Map.of("keys",java.util.List.of(new RSAKey.Builder(k.publicKey()).keyID(k.kid()).algorithm(com.nimbusds.jose.JWSAlgorithm.RS256).build().toJSONObject()));}}
