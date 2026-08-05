@echo off
cd /d "%~dp0"
if not defined SIDEKICK_PYTHON (
  if exist "E:\Programs\anaconda\envs\multiagent\python.exe" (
    set "SIDEKICK_PYTHON=E:\Programs\anaconda\envs\multiagent\python.exe"
  ) else if exist ".sidekick-python" (
    set /p SIDEKICK_PYTHON=<.sidekick-python
  )
)
if defined SIDEKICK_PYTHON (
  "%SIDEKICK_PYTHON%" main.py %*
) else (
  python main.py %*
)
