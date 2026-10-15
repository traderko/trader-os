@echo off
rem Same as "publish.bat release": commit + push, then tag the next version (latest on GitHub Releases + 1).
rem   release.bat 0.3.0   tag this exact version
call "%~dp0publish.bat" release %*
