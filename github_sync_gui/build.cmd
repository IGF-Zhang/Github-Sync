@echo off
echo Installing dependencies...
pip install -r requirements.txt

echo Building Executable...
pyinstaller --noconfirm --onefile --windowed --collect-all requests --collect-all urllib3 --hidden-import ssl --name "GithubSync" gui.py

echo Build complete! Check the 'dist' folder for GithubSync.exe
pause
