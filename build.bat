@echo off
chcp 65001 >nul
REM ============================================================
REM  Windows에서 한 번만 실행하면 dist\TableauPartsSync.exe 생성
REM  (인터넷 연결 + Python 3.9~3.12 설치 필요)
REM ============================================================

echo [1/4] 가상환경 생성...
python -m venv build_env
call build_env\Scripts\activate

echo [2/4] 라이브러리 설치...
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller

echo [3/4] exe 빌드...
pyinstaller --onefile --windowed --name "TableauPartsSync" ^
  --collect-all tableauhyperapi ^
  --collect-all pantab ^
  --collect-all tableauserverclient ^
  tableau_sync_gui.py

echo [4/4] 완료.
echo.
echo  ============================================
echo   결과물: dist\TableauPartsSync.exe
echo   이 exe 하나만 상대방에게 전달하면 됩니다.
echo  ============================================
pause
