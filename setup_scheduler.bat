@echo off
REM ============================================================================
REM YouTube Summary System - Windows Task Scheduler Setup
REM ============================================================================
REM This script creates a scheduled task to run the YouTube Summary System
REM daily at 11:00 PM, with an additional trigger on user logon for catch-up.
REM
REM IMPORTANT: Run this script as Administrator!
REM ============================================================================

setlocal enabledelayedexpansion

echo.
echo ============================================================
echo YouTube Summary System - Task Scheduler Setup
echo ============================================================
echo.

REM Check for admin privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo ERROR: This script requires Administrator privileges.
    echo.
    echo Please right-click on this file and select "Run as administrator"
    echo.
    pause
    exit /b 1
)

REM Configuration
set "TASK_NAME=YouTubeSummarySystem"
set "PROJECT_DIR=%~dp0"
set "PYTHON_SCRIPT=%PROJECT_DIR%src\main.py"

REM Remove trailing backslash from PROJECT_DIR
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

REM Find Python executable
where python >nul 2>&1
if %errorLevel% neq 0 (
    echo ERROR: Python not found in PATH.
    echo Please ensure Python is installed and added to PATH.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('where python') do (
    set "PYTHON_PATH=%%i"
    goto :found_python
)
:found_python

echo Configuration:
echo   Task Name: %TASK_NAME%
echo   Project Directory: %PROJECT_DIR%
echo   Python Path: %PYTHON_PATH%
echo   Script Path: %PYTHON_SCRIPT%
echo.

REM Check if task already exists
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if %errorLevel% equ 0 (
    echo Task "%TASK_NAME%" already exists.
    set /p "OVERWRITE=Do you want to delete and recreate it? (Y/N): "
    if /i "!OVERWRITE!"=="Y" (
        echo Deleting existing task...
        schtasks /delete /tn "%TASK_NAME%" /f
    ) else (
        echo Keeping existing task. Exiting.
        pause
        exit /b 0
    )
)

echo.
echo Creating scheduled task...
echo.

REM Create the scheduled task with daily trigger at 11:00 PM
REM Using schtasks with XML for more complex configurations

REM First, create with the daily trigger
schtasks /create ^
    /tn "%TASK_NAME%" ^
    /tr "\"%PYTHON_PATH%\" \"%PYTHON_SCRIPT%\"" ^
    /sc daily ^
    /st 23:00 ^
    /rl HIGHEST ^
    /f

if %errorLevel% neq 0 (
    echo ERROR: Failed to create scheduled task.
    pause
    exit /b 1
)

echo.
echo Task created successfully!
echo.

REM Now add logon trigger using PowerShell (schtasks doesn't support multiple triggers well)
echo Adding logon trigger for catch-up runs...

powershell -Command ^
    "$task = Get-ScheduledTask -TaskName '%TASK_NAME%'; ^
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn; ^
    $logonTrigger.Delay = 'PT5M'; ^
    $triggers = @($task.Triggers); ^
    $triggers += $logonTrigger; ^
    Set-ScheduledTask -TaskName '%TASK_NAME%' -Trigger $triggers" 2>nul

if %errorLevel% equ 0 (
    echo Logon trigger added successfully!
) else (
    echo Note: Could not add logon trigger. Daily schedule is still active.
)

echo.
echo ============================================================
echo Setup Complete!
echo ============================================================
echo.
echo The task "%TASK_NAME%" has been configured to:
echo   1. Run daily at 11:00 PM
echo   2. Run 5 minutes after user logon (for catch-up)
echo.
echo You can manage the task in Windows Task Scheduler:
echo   1. Press Win+R
echo   2. Type: taskschd.msc
echo   3. Look for "%TASK_NAME%" in the Task Scheduler Library
echo.
echo To run the task manually:
echo   schtasks /run /tn "%TASK_NAME%"
echo.
echo To delete the task:
echo   schtasks /delete /tn "%TASK_NAME%" /f
echo.
echo ============================================================
echo.
pause
