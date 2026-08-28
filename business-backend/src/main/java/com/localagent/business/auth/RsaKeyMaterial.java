package com.localagent.business.auth;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.*; import java.security.interfaces.*; import java.security.spec.*; import java.util.Base64;
import org.springframework.stereotype.Component;

@Component
public class RsaKeyMaterial {
  private final RSAPrivateKey privateKey; private final RSAPublicKey publicKey; private final String kid;
  public RsaKeyMaterial(AuthProperties p) {
    try {
      this.kid=p.keyId();
      if (p.privateKeyPem()!=null && !p.privateKeyPem().isBlank() && p.publicKeyPem()!=null && !p.publicKeyPem().isBlank()) {
        KeyFactory f=KeyFactory.getInstance("RSA");
        privateKey=(RSAPrivateKey)f.generatePrivate(new PKCS8EncodedKeySpec(decode(resolve(p.privateKeyPem()))));
        publicKey=(RSAPublicKey)f.generatePublic(new X509EncodedKeySpec(decode(resolve(p.publicKeyPem()))));
      } else if (p.allowEphemeralKey()) {
        KeyPairGenerator g=KeyPairGenerator.getInstance("RSA"); g.initialize(2048); KeyPair pair=g.generateKeyPair();
        privateKey=(RSAPrivateKey)pair.getPrivate(); publicKey=(RSAPublicKey)pair.getPublic();
      } else throw new IllegalStateException("JWT RSA private/public key configuration is required");
    } catch (GeneralSecurityException | IOException e) { throw new IllegalStateException("Invalid JWT RSA key configuration", e); }
  }
  private static String resolve(String value) throws IOException {
    if (!value.startsWith("file:")) return value;
    Path path=Path.of(value.substring("file:".length())).normalize();
    if (!path.isAbsolute()) throw new IllegalArgumentException("JWT key file path must be absolute");
    return Files.readString(path);
  }
  private static byte[] decode(String pem){ return Base64.getDecoder().decode(pem.replaceAll("-----[^-]+-----","").replaceAll("\\s", "")); }
  public RSAPrivateKey privateKey(){return privateKey;} public RSAPublicKey publicKey(){return publicKey;} public String kid(){return kid;}
}
