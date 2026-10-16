"""The isolated VRS release must never promote the Python bot or a public function."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/deploy_vrs_major_function.py'
spec=importlib.util.spec_from_file_location('vrs_deploy',SCRIPT)
deploy=importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def test_yc_access_binding_list_format_is_checked():
    public={'subject':{'type':'system','id':'allUsers'}}
    assert deploy.access_list([public])==[public]
    assert deploy.access_list({'access_bindings':[public]})==[public]


def test_promote_rejects_a_manifest_pointing_at_the_bot_before_any_tag_change(monkeypatch,tmp_path):
    manifest={'status':'candidate_validated','functionName':deploy.NAME,'functionId':deploy.BOT}
    file=tmp_path/'manifest.json';file.write_text(json.dumps(manifest))
    monkeypatch.setattr(deploy,'check',lambda:({'id':'bot-prod'},[],[{'id':'vrs-function'}]))
    monkeypatch.setattr(deploy,'yc',lambda *a,**k:pytest.fail('No mutation is allowed'))
    with pytest.raises(AssertionError,match='outside VRS scope'):
        deploy.promote(file)


def test_first_release_removes_production_tag_if_post_promote_smoke_fails(monkeypatch,tmp_path):
    manifest={'status':'candidate_validated','functionName':deploy.NAME,'functionId':'vrs-function',
              'candidateVersion':'vrs-candidate','previousVersion':None,'botProductionUnchanged':'bot-prod','timers':[]}
    file=tmp_path/'manifest.json';file.write_text(json.dumps(manifest))
    monkeypatch.setattr(deploy,'check',lambda:({'id':'bot-prod'},[],[{'id':'vrs-function'}]))
    monkeypatch.setattr(deploy,'tagged',lambda function,tag:{'id':'vrs-candidate'})
    calls=[]
    def yc(*args,**kwargs):
        calls.append(args)
        return [] if args[:4]==('serverless','function','version','list') else {}
    monkeypatch.setattr(deploy,'yc',yc)
    monkeypatch.setattr(deploy,'smoke',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('smoke failed')))
    with pytest.raises(RuntimeError,match='smoke failed'):
        deploy.promote(file)
    assert any(call[:4]==('serverless','function','version','remove-tag') for call in calls)
    assert not any(deploy.BOT in call for call in calls)
    assert json.loads(file.read_text())['status']=='rolled_back'
