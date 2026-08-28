package com.localagent.business.auth;
import java.util.Map; import org.springframework.http.*; import org.springframework.web.bind.MethodArgumentNotValidException; import org.springframework.web.bind.annotation.*;
@RestControllerAdvice public class ApiExceptionHandler {
 @ExceptionHandler(AuthException.class) ResponseEntity<Map<String,String>> auth(AuthException e){return ResponseEntity.status(e.status).body(Map.of("code",e.code,"message",e.getMessage()));}
 @ExceptionHandler(MethodArgumentNotValidException.class) ResponseEntity<Map<String,String>> validation(MethodArgumentNotValidException e){boolean verification=e.getBindingResult().getFieldErrors().stream().anyMatch(error->"verificationCode".equals(error.getField()));return ResponseEntity.unprocessableEntity().body(Map.of("code",verification?"invalid_verification_code":"validation_failed","message",verification?"邮箱验证码无效或已过期。":"请求字段无效。"));}
}
