@echo off
chcp 65001 >nul
title AIteller Weekly Pipeline
cd /d "%~dp0"

echo ============================================================
echo   AIteller Weekly Pipeline
echo   Date: %date% %time%
echo ============================================================
echo.

REM ── Step 1: Collect (all sources incl. Reddit, then filter+score) ──
REM Reddit now runs concurrently inside this collect (sources.reddit.enabled
REM is true; its per-sub checkpoint self-expires after checkpoint_ttl_hours,
REM so no "delete reddit_progress.json" step is needed). Phase 2 of collect
REM filters+scores everything, so the old separate rescore step is gone too.
echo [1/4] Collecting from all sources incl. Reddit (168h), then filter+score...
echo      This may take up to ~1 hour (Reddit is paced to avoid rate limits).
echo.
uv run python -m src.main collect
if errorlevel 1 (
    echo.
    echo [ERROR] Collection failed! Check logs/aiteller.log for details.
    echo Press any key to exit...
    pause >nul
    exit /b 1
)
echo.
echo [1/4] Collection + scoring complete!
echo.

REM ── Step 2: Chinese Weekly PDF ──────────────────────────────
echo [2/4] Generating Chinese weekly PDF...
uv run python -m src.main report weekly --lang zh
if errorlevel 1 (
    echo [WARNING] Chinese PDF generation failed, continuing...
)
echo.

REM ── Step 3: English Weekly PDF ──────────────────────────────
echo [3/4] Generating English weekly PDF...
uv run python -m src.main report weekly --lang en
if errorlevel 1 (
    echo [WARNING] English PDF generation failed, continuing...
)
echo.

REM ── Step 4: Send Emails ─────────────────────────────────────
echo [4/4 opt] Sending weekly PDFs to subscribers...
echo      Chinese PDF -> all subscribers (except EN-only)
echo      English PDF -> yuguangdang123@gmail.com
uv run python -m src.main send weekly
if errorlevel 1 (
    echo [WARNING] Email sending failed, continuing...
)
echo.

REM ── Step 5: Slideshow Video + Cover (Chinese only) ──────────
echo [video]  Generating slideshow video with rich cover (zh only)...
echo      This may take 10-20 minutes (per-item TTS + slide render + concat).
echo      English video is intentionally NOT generated.
uv run python -m src.main audio weekly --lang zh --video-mode slideshow
if errorlevel 1 (
    echo [WARNING] Audio/video generation failed, continuing...
)
echo.

REM ── Step 6: Status ──────────────────────────────────────────
echo [status] Final status:
uv run python -m src.main status
echo.

echo ============================================================
echo   Pipeline complete!
echo.
echo   Output files:
echo     PDF (zh):   output\weekly\AI*.pdf
echo     PDF (en):   output\weekly\AI_Practice_Weekly_*.pdf
echo     Cover PNG:  output\weekly\video\cover_*.png
echo     Video MP4:  output\weekly\video\slideshow_*.mp4
echo ============================================================
echo.
echo Press any key to exit...
pause >nul
