@echo off
pyinstaller --noconfirm --clean --onefile --windowed --name lockbox-gui --icon lockbox\assets\icon.ico --version-file version_info.txt --add-data "lockbox\assets;lockbox\assets" --collect-all tkinterdnd2 gui_entry.py
pyinstaller --noconfirm --clean --onefile --name lockbox --icon lockbox\assets\icon.ico --version-file version_info.txt cli_entry.py
