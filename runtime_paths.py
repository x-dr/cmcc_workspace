"""Read-only application resources and per-user writable desktop data."""
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
FROZEN=bool(getattr(sys,'frozen',False))
if os.environ.get('LINGXI_DATA_DIR'):
    DATA_DIR=Path(os.environ['LINGXI_DATA_DIR']).resolve()
elif not FROZEN:
    DATA_DIR=ROOT/'.local'
elif sys.platform == 'win32':
    DATA_DIR=Path(os.environ['LOCALAPPDATA'])/'LingxiWorkspace'
else:
    DATA_DIR=Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local/share')/'LingxiWorkspace'
RECHARGE_DATA_DIR=DATA_DIR/'recharge' if FROZEN or os.environ.get('LINGXI_DATA_DIR') else ROOT/'recharge/.local'
VERSION='1.9.4'
