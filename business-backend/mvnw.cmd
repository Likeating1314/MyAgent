@echo off
setlocal
set "MAVEN_VERSION=3.9.9"
set "MAVEN_DIR=%~dp0.mvn\apache-maven-%MAVEN_VERSION%"
if not exist "%MAVEN_DIR%\bin\mvn.cmd" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $zip='%TEMP%\apache-maven-%MAVEN_VERSION%.zip'; Invoke-WebRequest 'https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/%MAVEN_VERSION%/apache-maven-%MAVEN_VERSION%-bin.zip' -OutFile $zip; Expand-Archive -Force $zip '%~dp0.mvn'; Remove-Item -LiteralPath $zip"
  if errorlevel 1 exit /b 1
)
set "MAVEN_REPOSITORY=%USERPROFILE%\.m2\repository"
call "%MAVEN_DIR%\bin\mvn.cmd" "-Dmaven.repo.local=%MAVEN_REPOSITORY%" %*
exit /b %ERRORLEVEL%
