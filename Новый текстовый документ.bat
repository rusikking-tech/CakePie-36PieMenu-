@echo off
chcp 65001 >nul
title PieTest Launcher
echo ======================================
echo   PieTest - запуск радиального меню
echo ======================================
echo.
echo Лог пишется в файл PieTest.log
echo Запускаю...
echo.

REM === Путь к Python, измени при необходимости ===
set PYTHON_EXE=python

REM === Запуск скрипта и запись ошибок в лог ===
%PYTHON_EXE% PieTest.py >> PieTest.log 2>&1

echo.
echo Скрипт завершён. Проверь PieTest.log для отчёта.
pause
