from pathlib import Path
import hashlib,json,os,subprocess,sys,zipfile
ROOT=Path('/Users/user/.codex/worktrees/review-fixes-20261029/CS2results')
sys.path[:0]=[str(ROOT/'tests'),str(ROOT)]
from test_build_archives import package_source
OUT=Path('/private/tmp/cs2results-reaudit-media')
fixture=OUT/'old-builder-normal-fixture'
fixture.mkdir(exist_ok=True)
source=package_source.__wrapped__(fixture)
current=(source/'scripts/build_social_publish_zip.sh').read_text()
old=subprocess.run(['git','show','b363ac0:scripts/build_social_publish_zip.sh'],cwd=ROOT,check=True,text=True,capture_output=True).stdout
# A normal import, as test execution does, produces actual cache files.
env=dict(os.environ);env.pop('PYTHONDONTWRITEBYTECODE',None)
subprocess.run([sys.executable,'-c',"from cs2bot.match_sources.models import MatchNormalized"],cwd=source,env=env,check=True)
cache=list((source/'cs2bot/match_sources/__pycache__').glob('*.pyc'))
assert cache
archive=OUT/'old-builder-then-current-social.zip'
if archive.exists():archive.unlink()
builder=source/'scripts/build_social_publish_zip.sh'
builder.write_text(old)
subprocess.run([str(builder),str(archive)],cwd=source,check=True,capture_output=True,text=True)
with zipfile.ZipFile(archive) as z:
    old_cache=sorted(p for p in z.namelist() if p.endswith('.pyc'))
    assert old_cache and 'cs2bot/main.py' not in z.namelist()
builder.write_text(current)
subprocess.run([str(builder),str(archive)],cwd=source,check=True,capture_output=True,text=True)
with zipfile.ZipFile(archive) as z:
    current_cache=sorted(p for p in z.namelist() if p.endswith('.pyc'))
    assert current_cache==old_cache and 'cs2bot/main.py' not in z.namelist()
result={'old_script_ref':'b363ac0','old_script_sha256':hashlib.sha256(old.encode()).hexdigest(),
 'cache_creation':'ordinary import of match_sources.models with Python bytecode enabled',
 'old_zip_cache':old_cache,'rebuilt_zip_cache':current_cache,
 'main_in_either_zip':False,'severity':'P3','reproduced':True}
(OUT/'existing-archive-cache-proof.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
