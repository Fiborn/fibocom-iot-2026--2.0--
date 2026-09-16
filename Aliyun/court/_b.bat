@echo off
call "E:\visual Studio\Common7\Tools\VsDevCmd.bat" -arch=amd64 >nul 2>&1
cmake -S F:\animation_final\court -B F:\animation_final\court\out\build\x64-Debug -G Ninja 2>&1
cmake --build F:\animation_final\court\out\build\x64-Debug --config Debug 2>&1
