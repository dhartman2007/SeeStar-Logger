"""Launch the native Windows desktop logger."""
from pathlib import Path
import subprocess
if __name__=='__main__':
    subprocess.Popen([str(Path(__file__).with_name('SeestarLogger.exe'))])
