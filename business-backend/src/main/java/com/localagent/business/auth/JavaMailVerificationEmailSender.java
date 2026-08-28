package com.localagent.business.auth;

import org.springframework.mail.SimpleMailMessage;
import org.springframework.mail.javamail.JavaMailSender;
import org.springframework.stereotype.Component;

@Component
public class JavaMailVerificationEmailSender implements VerificationEmailSender {
  private final JavaMailSender mail; private final EmailVerificationProperties properties;
  public JavaMailVerificationEmailSender(JavaMailSender mail,EmailVerificationProperties properties){this.mail=mail;this.properties=properties;}
  @Override public void send(String email,String code,long minutes){SimpleMailMessage message=new SimpleMailMessage();message.setFrom(properties.mailFrom());message.setTo(email);message.setSubject("MyAgent 注册邮箱验证");message.setText("欢迎注册 MyAgent。\n\n您的注册验证码是："+code+"\n\n验证码在 "+minutes+" 分钟内有效。若非本人操作，请忽略此邮件。");mail.send(message);}
}
