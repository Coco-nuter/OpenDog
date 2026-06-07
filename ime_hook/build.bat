@echo off
setlocal

set OUTPUT=..\ime_hook_v3.dll
set SOURCE=ime_hook.cpp

echo ============================================================
echo Building ime_hook.dll
echo ============================================================

if defined INCLUDE goto :msvc

if exist "D:\VS2022\VC\Auxiliary\Build\vcvars64.bat" (
    call "D:\VS2022\VC\Auxiliary\Build\vcvars64.bat"
    goto :msvc
)

echo [FAIL] MSVC x64 build environment was not found.
echo Run this script from an x64 Native Tools Command Prompt.
exit /b 1

:msvc
cl.exe /nologo /LD /O2 /W3 /utf-8 ^
    /D WIN32 /D NDEBUG /D _WINDOWS /D _USRDLL ^
    %SOURCE% ^
    /Fe:%OUTPUT% ^
    user32.lib imm32.lib ^
    /link /SECTION:.shared,RWS

if not %ERRORLEVEL% == 0 (
    echo [FAIL] DLL compilation failed.
    exit /b 1
)

echo [OK] Built %OUTPUT%
exit /b 0
