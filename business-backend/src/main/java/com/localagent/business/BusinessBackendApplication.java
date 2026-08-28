package com.localagent.business;

import com.localagent.business.auth.AuthProperties;
import com.localagent.business.auth.EmailVerificationProperties;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.EnableConfigurationProperties;

@SpringBootApplication
@EnableConfigurationProperties({AuthProperties.class, EmailVerificationProperties.class})
public class BusinessBackendApplication {
  public static void main(String[] args) { SpringApplication.run(BusinessBackendApplication.class, args); }
}
