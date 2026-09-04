@echo off
setlocal
set "SCRIPT=%~dp0publish_skin_release.py"
set "PYTHON=E:\ChosenSkin2.0\Chosen\.venv\Scripts\python.exe"
if not defined CHOSEN_ROOT (
    if exist "E:\ChosenSkin2.0\Chosen\.worktrees\skin-incremental\src\core\skin_updater.py" (
        set "CHOSEN_ROOT=E:\ChosenSkin2.0\Chosen\.worktrees\skin-incremental"
    ) else (
        set "CHOSEN_ROOT=E:\ChosenSkin2.0\Chosen"
    )
)

if exist "%PYTHON%" (
    "%PYTHON%" "%SCRIPT%" %*
) else (
    py "%SCRIPT%" %*
)

if errorlevel 1 (
    echo.
    echo 发布工具启动失败，请检查 Python 环境和依赖。
    pause
)
endlocal
