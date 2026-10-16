"""Candidate -> smoke -> promote for the isolated private VRS function.

Uses only pinned Lockbox references from the current bot production version.
No secret payloads, social requests, timer changes or bot version changes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
YC=os.environ.get('YC_BIN',str(Path.home()/'yandex-cloud/bin/yc'))
FOLDER='b1g5j8hk4gjas2vpvgqr'
BOT='d4e6e13rlrl7go01m2q2'
NAME='cs2-vrs-major-reels'
PACKAGE_BUCKET='cs2results-function-packages-b1g5j8hk4gjas2vpvgqr'


def yc(*args,timeout=90):
    result=subprocess.run([YC,*args,'--format','json'],capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'yc {args[0]} operation failed: {result.stderr[:300]}')
    return json.loads(result.stdout or '{}')


def tagged(function,tag):
    return yc('serverless','function','version','get-by-tag','--function-id',function,'--tag',tag)


def access_list(response):
    return response if isinstance(response,list) else response.get('access_bindings',[])


def check():
    bot=yc('serverless','function','get',BOT)
    assert bot['folder_id']==FOLDER
    production=tagged(BOT,'production');assert production['status']=='ACTIVE'
    triggers=yc('serverless','trigger','list')
    for trigger in triggers:
        invoke=trigger.get('rule',{}).get('invoke_function',{})
        if invoke.get('function_id')==BOT:
            assert invoke.get('function_tag')=='production','Bot timer not pinned to production'
    functions=yc('serverless','function','list')
    existing=[f for f in functions if f['name']==NAME]
    assert len(existing)<=1
    if existing:
        assert existing[0]['folder_id']==FOLDER
        bindings=yc('serverless','function','list-access-bindings',existing[0]['id'])
        assert not any(b.get('subject',{}).get('id') in ['allUsers','allAuthenticatedUsers'] for b in access_list(bindings))
    bucket=yc('storage','bucket','get',PACKAGE_BUCKET,'--full')
    assert not any(bucket.get('anonymous_access_flags',{}).values()),'Package bucket is public'
    grants=bucket.get('acl',{}).get('grants',[])
    assert not any(g.get('grantee',{}).get('type') in ['GRANTEE_TYPE_ALL_USERS','GRANTEE_TYPE_ALL_AUTHENTICATED_USERS'] for g in grants)
    refs={s['environment_variable']:s for s in production.get('secrets',[])}
    assert all(refs.get(k,{}).get('version_id') for k in ['AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','LIQUIPEDIA_API_KEY'])
    return production,triggers,existing


def save(file,manifest):
    file.parent.mkdir(parents=True,exist_ok=True)
    file.write_text(json.dumps(manifest,ensure_ascii=False,indent=2))


def smoke(manifest,tag,probe=True):
    current=tagged(manifest['functionId'],tag)
    assert current['id']==manifest['candidateVersion']
    payload={'job':'render','region':'americas','dry_run':True,'render_probe':True} if probe else {'job':'capture','dry_run':True}
    response=yc('serverless','function','invoke',manifest['functionId'],'--tag',tag,'--data',json.dumps(payload),timeout=650)
    assert response.get('statusCode')==200,'Smoke invocation failed'
    body=json.loads(response['body']) if isinstance(response['body'],str) else response['body']
    assert body.get('dry_run') is True
    if probe:assert body.get('render',{}).get('frames')==1944 and body['render']['fullDecode']=='passed'
    return body


def candidate():
    assert not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip(),'Candidate requires a clean checkout'
    git_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    production,triggers,existing=check()
    previous=None
    if existing:
        versions=yc('serverless','function','version','list','--function-id',existing[0]['id'])
        old=[v for v in versions if 'production' in v.get('tags',[])]
        assert len(old)<=1
        previous=old[0]['id'] if old else None
    print('Preflight passed; building the isolated Linux archive',flush=True)
    subprocess.run([sys.executable,str(ROOT/'scripts/build_vrs_major_function.py')],cwd=ROOT,check=True)
    assert git_sha==subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    archive=ROOT/'dist/vrs-major-function.zip';digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    key=f'function-packages/vrs-major/{git_sha}/{digest}.zip'
    yc('storage','s3api','put-object','--bucket',PACKAGE_BUCKET,'--key',key,'--body',str(archive),'--acl','private','--content-type','application/zip',timeout=180)
    function=existing[0] if existing else yc('serverless','function','create',NAME,'--folder-id',FOLDER,'--description','Private VRS history capture and regional Reel rendering; no social delivery')
    refs={s['environment_variable']:s for s in production['secrets']}
    args=['serverless','function','version','create','--function-id',function['id'],'--runtime','nodejs22','--entrypoint','index.handler',
          '--memory','1024MB','--execution-timeout','600s','--concurrency','1','--service-account-id',production['service_account_id'],
          '--tags','candidate','--package-bucket-name',PACKAGE_BUCKET,'--package-object-name',key,'--package-sha256',digest,
          '--environment',f"OBJECT_STORAGE_BUCKET={production['environment']['OBJECT_STORAGE_BUCKET']}",
          '--description',f'VRS release git={git_sha} sha256={digest}']
    for name in ['AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','LIQUIPEDIA_API_KEY']:
        ref=refs[name]
        args+=['--secret',f"id={ref['id']},version-id={ref['version_id']},key={ref['key']},environment-variable={name}"]
    print('Creating private candidate; bot production is unchanged',flush=True)
    version=yc(*args,timeout=600);assert version['status']=='ACTIVE'
    manifest={'functionId':function['id'],'functionName':NAME,'candidateVersion':version['id'],'previousVersion':previous,
              'botProductionUnchanged':production['id'],'gitSha':git_sha,'packageSha256':digest,'packageBucket':PACKAGE_BUCKET,'packageObject':key,
              'timers':triggers,'status':'candidate_created'}
    file=ROOT/f"dist/vrs-releases/{version['id']}.json";save(file,manifest)
    print(f'Candidate created: {version["id"]}; rendering the full private probe',flush=True)
    manifest['candidateProbe']=smoke(manifest,'candidate')
    manifest['candidateSourceCheck']=smoke(manifest,'candidate',probe=False)
    manifest['status']='candidate_validated';save(file,manifest)
    print(json.dumps({'manifest':str(file),'candidateVersion':version['id'],'status':manifest['status']}),flush=True)


def promote(file):
    manifest=json.loads(file.read_text());assert manifest['status']=='candidate_validated'
    production,triggers,existing=check()
    assert manifest['functionName']==NAME and existing and existing[0]['id']==manifest['functionId'],'Manifest is outside VRS scope'
    assert production['id']==manifest['botProductionUnchanged'] and triggers==manifest['timers'],'Unrelated production changed'
    assert tagged(manifest['functionId'],'candidate')['id']==manifest['candidateVersion']
    if manifest['previousVersion']:
        assert tagged(manifest['functionId'],'production')['id']==manifest['previousVersion']
        yc('serverless','function','version','set-tag','--id',manifest['previousVersion'],'--tag','rollback')
    else:
        versions=yc('serverless','function','version','list','--function-id',manifest['functionId'])
        assert not any('production' in v.get('tags',[]) for v in versions),'Production appeared after candidate'
    yc('serverless','function','version','set-tag','--id',manifest['candidateVersion'],'--tag','production')
    try:
        manifest['productionSmoke']=smoke(manifest,'production',probe=False)
        assert tagged(BOT,'production')['id']==manifest['botProductionUnchanged']
        assert yc('serverless','trigger','list')==manifest['timers']
        access=yc('serverless','function','list-access-bindings',manifest['functionId'])
        assert not any(b.get('subject',{}).get('id') in ['allUsers','allAuthenticatedUsers'] for b in access_list(access))
    except Exception:
        if manifest['previousVersion']:
            yc('serverless','function','version','set-tag','--id',manifest['previousVersion'],'--tag','production')
        else:
            yc('serverless','function','version','remove-tag','--id',manifest['candidateVersion'],'--tag','production')
        manifest['status']='rolled_back';save(file,manifest);raise
    manifest['status']='promoted';save(file,manifest)
    print(json.dumps({'functionId':manifest['functionId'],'productionVersion':manifest['candidateVersion'],'status':'promoted','botAndTimersUnchanged':True}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['check','candidate','promote']);parser.add_argument('manifest',nargs='?',type=Path)
    args=parser.parse_args()
    if args.command=='check':
        production,triggers,existing=check();print(json.dumps({'botProduction':production['id'],'timers':len(triggers),'vrsExists':bool(existing),'check':'passed'}))
    elif args.command=='candidate':candidate()
    else:
        assert args.manifest,'Manifest is required'
        assert os.environ.get('YC_PROMOTE_APPROVED')=='1','Set YC_PROMOTE_APPROVED=1 after release approval'
        promote(args.manifest)
