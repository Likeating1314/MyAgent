package com.localagent.business.auth;
public interface VerificationEmailSender { void send(String normalizedEmail,String code,long expiresInMinutes); }
