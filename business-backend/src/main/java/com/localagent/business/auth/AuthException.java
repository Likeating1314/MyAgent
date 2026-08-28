package com.localagent.business.auth;
import org.springframework.http.HttpStatus;
public class AuthException extends RuntimeException { final String code; final HttpStatus status; public AuthException(String c,String m,HttpStatus s){super(m);code=c;status=s;} }
