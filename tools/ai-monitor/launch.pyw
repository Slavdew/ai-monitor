"""Значок в трее без окна консоли: pythonw окна не имеет, powershell с CREATE_NO_WINDOW — тоже.
`start powershell -WindowStyle Hidden` при Windows Terminal как консоли по умолчанию оставлял свёрнутое окно
терминала, в которое ничего не ввести (28.10). Аргумент /silent — без браузера (автозагрузка, установщик)."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
args = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', os.path.join(HERE, 'tray.ps1')]
if '/silent' not in (a.lower() for a in sys.argv[1:]):
    args.append('-Open')
subprocess.Popen(args, cwd=HERE, creationflags=subprocess.CREATE_NO_WINDOW)
