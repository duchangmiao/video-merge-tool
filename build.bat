@echo off
chcp 65001 >nul
cd /d %~dp0

echo ============================================================
echo  视频合并工具 - 绿色便携版打包（onedir，内置 Python 运行时 + ffmpeg）
echo ============================================================

echo.
echo [1/4] 检查并安装 PyInstaller ...
python -m pip install --disable-pip-version-check pyinstaller
if errorlevel 1 (
    echo [错误] PyInstaller 安装失败，请检查网络或 Python 环境。
    pause
    exit /b 1
)

echo.
echo [2/4] 调用 PyInstaller 打包（窗口模式 / 目录版）...
python -m PyInstaller --noconfirm --windowed --onedir --name VideoMergeQSV app.py
if errorlevel 1 (
    echo [错误] PyInstaller 打包失败。
    pause
    exit /b 1
)

echo.
echo [3/4] 拷贝内置 ffmpeg / ffprobe 到便携目录 ...
robocopy ffmpeg "dist\VideoMergeQSV\ffmpeg" /E /NFL /NDL /NJH /NJS
if errorlevel 8 (
    echo [错误] ffmpeg 拷贝失败。
    pause
    exit /b 1
)

echo.
echo [4/4] 压缩为便携 zip ...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\VideoMergeQSV\*' -DestinationPath 'dist\VideoMergeQSV_portable.zip' -Force"

echo.
echo ============================================================
echo  打包完成：
echo   便携目录: dist\VideoMergeQSV\VideoMergeQSV.exe
echo   压缩包:   dist\VideoMergeQSV_portable.zip
echo  把整个 VideoMergeQSV 文件夹（或解压 zip）拷到目标机即可运行，
echo  目标机无需安装 Python。ffmpeg\bin 随目录一起携带。
echo ============================================================
pause
