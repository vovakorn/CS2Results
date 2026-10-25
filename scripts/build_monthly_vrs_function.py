"""Build only the isolated monthly publisher's Python package; no deployment."""
import os
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build():
    # Reuse the project's pinned proxy binary and checksum verification.
    xray = ROOT / 'build/xray-core/v26.6.27/xray'
    if os.environ.get('XRAY_ENABLED') == '1' and not xray.exists():
        subprocess.run([str(ROOT / 'scripts/fetch_xray_core.sh')], check=True)
    output = ROOT / 'dist/monthly-vrs-function.zip'
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / 'cs2bot').rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                archive.write(path, path.relative_to(ROOT))
        for name in ['requirements.txt', 'runtime.txt']:
            archive.write(ROOT / name, name)
        archive.write(ROOT / 'data/reel-safe-zone.json', 'data/reel-safe-zone.json')
        if os.environ.get('XRAY_ENABLED') == '1':
            archive.write(xray, 'xray/xray')
    print(output)
    return output


if __name__ == '__main__':
    build()
