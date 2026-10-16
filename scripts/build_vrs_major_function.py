"""Build the isolated Linux x64 VRS function; the Python bot archive is untouched."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'video/vrs-major-race'
OUTPUT=ROOT/'build/vrs-major-function'
CACHE=ROOT/'build/vendor'
DIST=ROOT/'dist'


def build():
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)
    CACHE.mkdir(parents=True,exist_ok=True)
    DIST.mkdir(exist_ok=True)
    node=shutil.which('node') or str(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')
    subprocess.run([node,str(SOURCE/'cloud/build.mjs'),str(OUTPUT)],check=True)
    assets=OUTPUT/'assets';assets.mkdir()
    for name in ['Arimo-Bold.ttf','Arimo-OFL.txt','road-to-major.wav','music-manifest.json','logos/manifest.json']:
        target=assets/name;target.parent.mkdir(exist_ok=True);shutil.copyfile(SOURCE/'assets'/name,target)
    for name in ['RussoOne-Regular.ttf','RussoOne-OFL.txt']:
        shutil.copyfile(ROOT/'cs2bot/assets/fonts'/name,assets/name)
    shutil.copyfile(ROOT/'cs2bot/assets/channel-logo.png',assets/'channel-logo.png')
    fixtures=OUTPUT/'fixtures';fixtures.mkdir()
    shutil.copyfile(SOURCE/'fixtures/liquipedia-history.json.gz',fixtures/'liquipedia-history.json.gz')
    vendor=json.loads((SOURCE/'assets/vendor.json').read_text())['ffmpeg']
    wheel=CACHE/vendor['filename']
    if not wheel.exists() or hashlib.sha256(wheel.read_bytes()).hexdigest()!=vendor['sha256']:
        with urllib.request.urlopen(vendor['url'],timeout=60) as response:
            wheel.write_bytes(response.read(40*1024*1024))
    assert hashlib.sha256(wheel.read_bytes()).hexdigest()==vendor['sha256'],'FFmpeg vendor checksum mismatch'
    binary=OUTPUT/'bin';binary.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        name=next(n for n in archive.namelist() if '/binaries/ffmpeg-linux-x86_64-' in n)
        (binary/'ffmpeg').write_bytes(archive.read(name))
        license_name=next(n for n in archive.namelist() if n.endswith('LICENSE') or n.endswith('LICENSE.txt'))
        (binary/'imageio-ffmpeg-LICENSE.txt').write_bytes(archive.read(license_name))
    os.chmod(binary/'ffmpeg',0o755)
    git_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    (OUTPUT/'build-info.json').write_text(json.dumps({'gitSha':git_sha,'ffmpeg':vendor,'runtime':'nodejs22'},indent=2))
    target=DIST/'vrs-major-function.zip'
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(OUTPUT.rglob('*')):
            if file.is_file():archive.write(file,file.relative_to(OUTPUT))
    print(json.dumps({'file':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'gitSha':git_sha}))
    return target


if __name__=='__main__':
    build()
