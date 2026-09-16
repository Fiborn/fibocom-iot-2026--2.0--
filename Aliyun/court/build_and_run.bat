@echo off
chcp 65001 >nul
title BWF Court 3D Generator

set BUILD_DIR=%~dp0build
set SRC_DIR=%~dp0src
set COURT_DIR=%~dp0

echo ==================================================
echo  Building BWF Badminton Court 3D Model Generator
echo ==================================================

if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"
cd /d "%BUILD_DIR%"

echo [1/2] Configuring with CMake...
cmake .. -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo CMake configuration failed!
    echo Trying direct g++ compilation...
    cd /d "%COURT_DIR%"
    goto :direct_build
)

echo [2/2] Building...
cmake --build . --config Release

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo CMake build failed! Trying direct compilation...
    cd /d "%COURT_DIR%"
    goto :direct_build
)

echo.
echo Build successful! Running...
echo.
copy /Y "%BUILD_DIR%\court_generator.exe" "%COURT_DIR%\court_generator.exe" >nul
"%COURT_DIR%\court_generator.exe"
goto :end

:direct_build
echo.
echo === Direct g++ compilation ===
cd /d "%COURT_DIR%"

g++ -std=c++17 -O2 ^
    -I. -Iglad -IGLFW -IKHR -Iglm ^
    src/main.cpp src/court_model.cpp src/net_model.cpp src/shader.cpp src/obj_exporter.cpp glad/glad.c ^
    GLFW/libglfw3.a -lopengl32 -lgdi32 ^
    -o court_generator.exe

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: Build failed!
    pause
    exit /b 1
)

echo.
echo Build successful! Running...
echo.
court_generator.exe

:end
echo.
pause
