@rem Gradle wrapper bootstrap for Windows
@if "%DEBUG%"=="" @echo off
@rem Look for gradlew in current dir and parent dir
if exist "%~dp0gradle\wrapper\gradle-wrapper.jar" goto runGradle
@rem If wrapper jar doesn't exist, try to run with system gradle
echo Gradle wrapper not found. Please generate it by running:
echo   gradle wrapper
echo Or open this project in Android Studio.
exit /b 1

:runGradle
set DIRNAME=%~dp0
set APP_HOME=%DIRNAME%
set GRADLE_USER_HOME=%APP_HOME%gradle\home
java -jar "%APP_HOME%gradle\wrapper\gradle-wrapper.jar" %*
