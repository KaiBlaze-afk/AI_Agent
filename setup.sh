#!/usr/bin/env bash
# ==============================================================================
# Aria // Venice Agent Workspace - Automated Environment Setup
# ==============================================================================
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BOLD}======================================================${NC}"
echo -e "${BOLD}   Aria // Venice Agent Workspace Setup (macOS MLX)   ${NC}"
echo -e "${BOLD}======================================================${NC}"

# 1. Verify macOS
OS="$(uname -s)"
if [[ "$OS" != "Darwin" ]]; then
    echo -e "${RED}[ERROR] This agent uses Apple Silicon MLX acceleration, which requires macOS.${NC}"
    echo -e "Detected OS: $OS"
    exit 1
fi

# 2. Check Apple Silicon Architecture
ARCH="$(uname -m)"
if [[ "$ARCH" != "arm64" ]]; then
    echo -e "${YELLOW}[WARNING] Apple Silicon (M1/M2/M3/M4 arm64) is strongly recommended for MLX.${NC}"
    echo -e "Detected architecture: $ARCH"
fi

# 3. Locate Python 3.10+
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY_VER="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
        PY_MAJOR="$("$candidate" -c 'import sys; print(sys.version_info.major)')"
        PY_MINOR="$("$candidate" -c 'import sys; print(sys.version_info.minor)')"
        if [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -ge 10 ]]; then
            PYTHON_BIN="$candidate"
            echo -e "${GREEN}[OK]${NC} Found compatible Python: $PYTHON_BIN (v$PY_VER)"
            break
        fi
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    echo -e "${RED}[ERROR] Python 3.10 or higher is required. Please install it via Homebrew:${NC}"
    echo -e "  brew install python@3.12"
    exit 1
fi

# 4. Create Virtual Environment
if [[ ! -d ".venv" ]]; then
    echo -e "\n${BOLD}[1/5] Creating Python virtual environment in .venv...${NC}"
    "$PYTHON_BIN" -m venv .venv
else
    echo -e "\n${BOLD}[1/5] Virtual environment .venv already exists.${NC}"
fi

# Activate venv
# shellcheck disable=SC1091
source .venv/bin/activate

# 5. Upgrade pip and install dependencies
echo -e "\n${BOLD}[2/5] Installing dependencies from requirements.txt...${NC}"
pip install --upgrade pip
pip install -r requirements.txt

# 6. Install Playwright Chromium for Media Sniffing
echo -e "\n${BOLD}[3/5] Installing Playwright Chromium browser...${NC}"
playwright install chromium || echo -e "${YELLOW}[NOTICE] Playwright Chromium install encountered an issue; stream sniffer may be limited.${NC}"

# 7. Check for ffmpeg (used by yt-dlp for audio conversion)
echo -e "\n${BOLD}[4/5] Checking optional system tools...${NC}"
if command -v ffmpeg >/dev/null 2>&1; then
    echo -e "${GREEN}[OK]${NC} ffmpeg is installed."
else
    echo -e "${YELLOW}[TIP] ffmpeg is not installed. For audio conversion in yt-dlp, install via Homebrew:${NC}"
    echo -e "  brew install ffmpeg"
fi

# 8. Configure Environment and Workspace
echo -e "\n${BOLD}[5/5] Initializing workspace & configuration...${NC}"
mkdir -p Workspace
touch Workspace/.gitkeep

if [[ ! -f ".env" ]]; then
    cp .env.example .env
    echo -e "${GREEN}[OK]${NC} Created .env from .env.example"
else
    echo -e "${GREEN}[OK]${NC} Existing .env detected."
fi

echo -e "\n${BOLD}======================================================${NC}"
echo -e "${GREEN}${BOLD}Setup completed successfully!${NC}"
echo -e "${BOLD}======================================================${NC}"
echo -e "To launch Aria Venice Agent Workspace:"
echo -e "  ${BOLD}./run.sh${NC}"
echo -e "Or manually:"
echo -e "  source .venv/bin/activate"
echo -e "  python app.py"
echo -e "\nThen open your browser to: ${BOLD}http://127.0.0.1:5050${NC}\n"

