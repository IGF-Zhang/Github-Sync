@echo off
echo Installing dependencies...
pip install -r requirements.txt

echo Building Executable...
pyinstaller --noconfirm --onedir --windowed --name "GithubSync" gui.py

echo Build complete! Check the 'dist' folder for GithubSync.exe
pause
