package com.localagent.business.auth;
import java.time.*; import java.util.ArrayDeque; import java.util.concurrent.ConcurrentHashMap; import org.springframework.http.HttpStatus; import org.springframework.stereotype.Component;
@Component public class RateLimiter {
  private final AuthProperties p; private final ConcurrentHashMap<String,ArrayDeque<Instant>> buckets=new ConcurrentHashMap<>();
  public RateLimiter(AuthProperties p){this.p=p;}
  public void check(String key){ Instant now=Instant.now(), cutoff=now.minus(p.rateLimitWindow()); ArrayDeque<Instant> q=buckets.computeIfAbsent(key,k->new ArrayDeque<>()); synchronized(q){while(!q.isEmpty()&&q.peekFirst().isBefore(cutoff))q.removeFirst(); if(q.size()>=p.rateLimitAttempts())throw new AuthException("rate_limited","请求过于频繁，请稍后重试。",HttpStatus.TOO_MANY_REQUESTS); q.addLast(now);} }
}
