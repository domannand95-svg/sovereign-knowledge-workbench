"""Synthetic retry, receipt and ledger integration contracts; no live services."""
import ast
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sovereign_workbench import adapters, cli, staging


class RetryContracts(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='retry-contract-', dir=os.environ.get('WB_RETRY_SCRATCH')))
        self.source = self.root / 'source.txt'
        self.source.write_bytes(b'synthetic source\n')
        self.stage_root = self.root / 'stage'
        self.stage_root.mkdir()
        self.plan = staging.build_plan(self.source, self.stage_root)
        self.path = self.root / 'state.db'
        self.db = staging.connect(self.path)
        self.addCleanup(self.db.close)

    def receipt(self, plan):
        # Direct-module tests exercise binding only, not authenticity.
        return {'operation':'stage_copy','target':plan['staged_path'],'proposal_sha256':'0'*64}

    def fail_before_publication(self):
        self.source.write_bytes(b'drift')
        with self.assertRaises(staging.StagingError):
            staging.execute(self.db, self.plan, self.receipt(self.plan))
        self.source.write_bytes(b'synthetic source\n')
        return self.snapshot(self.plan['plan_id'])

    def snapshot(self, plan_id):
        cursor = self.db.execute('SELECT * FROM staged_operations WHERE plan_id=?', (plan_id,))
        return dict(zip((c[0] for c in cursor.description), cursor.fetchone()))

    def retry(self, parent=None):
        return staging.prepare_retry(self.db, parent or self.plan['plan_id'], reason='Synthetic reviewed retry proposal')

    def test_retry_is_new_linked_inert_attempt_and_parent_is_unchanged(self):
        before = self.fail_before_publication()
        child = self.retry()
        self.assertNotEqual(child['plan_id'], self.plan['plan_id'])
        self.assertNotEqual(child['staged_path'], self.plan['staged_path'])
        self.assertEqual(child['authority'], 'none')
        self.assertEqual(self.snapshot(self.plan['plan_id']), before)
        self.assertEqual(self.snapshot(child['plan_id'])['status'], 'planned')
        link = self.db.execute('SELECT parent_plan_id,parent_snapshot_json,parent_snapshot_sha256,authority FROM staging_retry_links').fetchone()
        self.assertEqual(link[0], self.plan['plan_id'])
        self.assertEqual(json.loads(link[1]), before)
        self.assertEqual(hashlib.sha256(link[1].encode()).hexdigest(), link[2])
        self.assertEqual(child['parent_snapshot_sha256'], link[2])
        self.assertEqual(link[3], 'none')
        self.assertFalse(Path(child['staged_path']).exists())
        self.assertIsNone(self.snapshot(child['plan_id'])['receipt_json'])

    def test_retry_executes_only_as_new_attempt_and_rolls_back_separately(self):
        before = self.fail_before_publication(); child = self.retry()
        staging.execute(self.db, child, self.receipt(child))
        self.assertEqual(self.snapshot(self.plan['plan_id']), before)
        self.assertEqual(Path(child['staged_path']).read_bytes(), self.source.read_bytes())
        with self.assertRaises(staging.StagingError):staging.rollback(self.db, child['plan_id'], {})
        staging.rollback(self.db, child['plan_id'], {'operation':'rollback_stage_copy','target':child['staged_path']})
        self.assertFalse(Path(child['staged_path']).exists())
        self.assertEqual(self.snapshot(self.plan['plan_id']), before)
        self.assertEqual(self.source.read_bytes(), b'synthetic source\n')

    def test_uncertain_or_legacy_parent_stays_blocked_even_if_target_absent(self):
        for phase in [None, 'publication_pending', 'published']:
            with self.subTest(phase=phase):
                if self.db.execute('SELECT 1 FROM staged_operations').fetchone():
                    # Independent database per subtest; no deleting attempt evidence.
                    self.db.close();self.path=self.root/('db-'+str(phase));self.db=staging.connect(self.path)
                before = self.fail_before_publication()
                self.db.execute('UPDATE staged_operations SET publication_state=?',(phase,));self.db.commit()
                before=self.snapshot(self.plan['plan_id']);child=self.retry()
                self.assertFalse(Path(self.plan['staged_path']).exists())
                with self.assertRaisesRegex(staging.StagingError,'uncertain or published'):
                    staging.execute(self.db, child, self.receipt(child))
                self.assertEqual(self.snapshot(self.plan['plan_id']),before)
                self.assertEqual(self.snapshot(child['plan_id'])['status'],'planned')
                self.assertFalse(Path(child['staged_path']).exists())

    def test_existing_parent_destination_blocks_retry_without_deleting_it(self):
        self.fail_before_publication();target=Path(self.plan['staged_path']);target.parent.mkdir();target.write_bytes(b'foreign evidence')
        child=self.retry()
        with self.assertRaisesRegex(staging.StagingError,'destination exists'):
            staging.execute(self.db,child,self.receipt(child))
        self.assertEqual(target.read_bytes(),b'foreign evidence')
        self.assertFalse(Path(child['staged_path']).exists())

    def test_partial_and_parent_receipts_preserved_through_retry(self):
        before=self.fail_before_publication();target=Path(self.plan['staged_path']);target.parent.mkdir();partial=target.with_suffix(target.suffix+'.partial');partial.write_bytes(b'preserved partial')
        child=self.retry();staging.execute(self.db,child,self.receipt(child))
        self.assertEqual(partial.read_bytes(),b'preserved partial')
        self.assertEqual(self.snapshot(self.plan['plan_id']),before)

    def test_parent_mutation_invalidates_recorded_retry(self):
        self.fail_before_publication();child=self.retry()
        self.db.execute("UPDATE staged_operations SET error='different evidence' WHERE plan_id=?",(self.plan['plan_id'],));self.db.commit()
        with self.assertRaisesRegex(staging.StagingError,'Parent attempt changed'):
            staging.execute(self.db,child,self.receipt(child))
        self.assertFalse(Path(child['staged_path']).exists())

    def test_link_is_immutable_and_a_second_sibling_is_not_created(self):
        self.fail_before_publication();self.retry()
        before=self.db.execute('SELECT * FROM staging_retry_links').fetchall()
        for statement in ['UPDATE staging_retry_links SET reason=reason','DELETE FROM staging_retry_links']:
            with self.assertRaises(sqlite3.IntegrityError):self.db.execute(statement)
            self.db.rollback()
        with self.assertRaisesRegex(staging.StagingError,'already has a linked retry'):self.retry()
        self.assertEqual(self.db.execute('SELECT * FROM staging_retry_links').fetchall(),before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM staged_operations').fetchone()[0],2)

    def test_retry_chain_preserves_each_failed_parent(self):
        root_before=self.fail_before_publication();child=self.retry()
        self.source.write_bytes(b'drift')
        with self.assertRaises(staging.StagingError):staging.execute(self.db,child,self.receipt(child))
        self.source.write_bytes(b'synthetic source\n');child_before=self.snapshot(child['plan_id'])
        grandchild=self.retry(child['plan_id']);staging.execute(self.db,grandchild,self.receipt(grandchild))
        self.assertEqual(grandchild['retry_of_plan_id'],child['plan_id'])
        self.assertEqual(self.snapshot(child['plan_id']),child_before)
        self.assertEqual(self.snapshot(self.plan['plan_id']),root_before)

    def test_forged_or_unrecorded_retry_plan_cannot_execute(self):
        self.fail_before_publication();child=self.retry()
        other=staging.connect(self.root/'other.db')
        try:
            with self.assertRaisesRegex(staging.StagingError,'parent link'):staging.execute(other,child,self.receipt(child))
        finally:other.close()
        altered=dict(child);altered['retry_of_plan_id']='0'*64
        with self.assertRaises(staging.StagingError):staging.verify_plan(altered)

    def test_plan_preparation_failure_is_atomic(self):
        self.fail_before_publication();self.source.rename(self.root/'preserved-source')
        with self.assertRaises(FileNotFoundError):self.retry()
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM staged_operations').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM staging_retry_links').fetchone()[0],0)

    def signed_authorizer(self, ledger, *, corrupt=False):
        key=Ed25519PrivateKey.generate()
        env={'SKW_SOVEREIGN_AUTHORIZER':'inert-not-executed','SKW_REQUESTER_ID':'synthetic-requester',
             'SKW_SOVEREIGN_VERIFYING_KEY':key.public_key().public_bytes_raw().hex(),
             'SKW_GRANT_LEDGER':str(ledger)}
        seen=[]
        def responder(*args, **kwargs):
            proposal=json.loads(kwargs['input']);seen.append(proposal)
            now=datetime.now(timezone.utc)-timedelta(seconds=1)
            receipt={'contract_version':'sovereign.authorization.receipt.v3','authorized':True,
                'grant_id':secrets.token_hex(32),'proposal_sha256':hashlib.sha256(kwargs['input']).hexdigest(),
                'operation':proposal['operation'],'target':proposal['target'],'policy_id':'synthetic-policy',
                'requester_identity':proposal['requester_identity'],'capability_request_id':secrets.token_hex(32),
                'authorization_nonce':proposal['authorization_nonce'],'issued_at':now.isoformat(),
                'expires_at':(now+timedelta(seconds=300)).isoformat(),'verifying_key':env['SKW_SOVEREIGN_VERIFYING_KEY']}
            receipt['signature']=key.sign(adapters._receipt_payload(receipt)).hex()
            if corrupt:receipt['signature']='00'*64
            return SimpleNamespace(returncode=0,stdout=json.dumps(receipt).encode())
        return env,responder,seen

    def test_actual_cli_fresh_signed_receipt_and_ledger_for_retry(self):
        parent_before=self.fail_before_publication();child=self.retry();path=self.root/'plan.json';path.write_text(json.dumps(child))
        ledger=self.root/'grant-ledger.db';env,responder,seen=self.signed_authorizer(ledger)
        with patch.dict(os.environ,env,clear=True),patch.object(adapters.subprocess,'run',responder),redirect_stdout(io.StringIO()):
            result=cli.main(['stage-execute','--plan',str(path),'--state-db',str(self.path)])
        self.assertEqual(result,0);self.assertEqual(len(seen),1)
        self.assertEqual(seen[0]['report_sha256'],child['plan_id'])
        self.assertEqual(seen[0]['target'],child['staged_path'])
        with sqlite3.connect(ledger) as db:
            rows=db.execute('SELECT receipt_json FROM consumed_grants').fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(json.loads(rows[0][0]),json.loads(self.snapshot(child['plan_id'])['receipt_json']))
        self.assertEqual(self.snapshot(self.plan['plan_id']),parent_before)

    def test_actual_cli_rejects_uncertain_retry_before_calling_authorizer(self):
        self.fail_before_publication();self.db.execute("UPDATE staged_operations SET publication_state='publication_pending'");self.db.commit();child=self.retry()
        path=self.root/'blocked.json';path.write_text(json.dumps(child))
        with patch.object(cli,'request_sovereign_authorization',side_effect=AssertionError('must not authorize')),redirect_stderr(io.StringIO()):
            result=cli.main(['stage-execute','--plan',str(path),'--state-db',str(self.path)])
        self.assertEqual(result,3);self.assertFalse(Path(child['staged_path']).exists())

    def test_actual_cli_bad_signature_cannot_execute_or_consume(self):
        self.fail_before_publication();child=self.retry();path=self.root/'plan.json';path.write_text(json.dumps(child))
        ledger=self.root/'grants.db';env,responder,seen=self.signed_authorizer(ledger,corrupt=True)
        with patch.dict(os.environ,env,clear=True),patch.object(adapters.subprocess,'run',responder),redirect_stderr(io.StringIO()):
            result=cli.main(['stage-execute','--plan',str(path),'--state-db',str(self.path)])
        self.assertEqual(result,3);self.assertFalse(ledger.exists())
        self.assertFalse(Path(child['staged_path']).exists())
        self.assertIsNone(self.snapshot(child['plan_id'])['receipt_json'])

    def test_actual_adapter_rejects_grant_and_nonce_replay(self):
        ledger=self.root/'grants.db';env,responder,seen=self.signed_authorizer(ledger)
        proposal={'operation':'stage_copy','target':'synthetic-target','authorization_nonce':'ab'*32}
        held=[]
        def same(*args,**kwargs):
            if not held:held.append(responder(*args,**kwargs))
            return held[0]
        with patch.dict(os.environ,env,clear=True),patch.object(adapters.subprocess,'run',same):
            adapters.request_sovereign_authorization(proposal)
            with self.assertRaisesRegex(adapters.AdapterError,'already consumed'):adapters.request_sovereign_authorization(proposal)
        # New grant with same nonce also fails the actual ledger's UNIQUE guard.
        with patch.dict(os.environ,env,clear=True),patch.object(adapters.subprocess,'run',responder):
            with self.assertRaisesRegex(adapters.AdapterError,'already consumed'):adapters.request_sovereign_authorization(proposal)
        with sqlite3.connect(ledger) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM consumed_grants').fetchone()[0],1)

    def test_cli_retry_plan_records_only_and_never_calls_authorizer(self):
        before=self.fail_before_publication();out=io.StringIO()
        with patch.object(cli,'request_sovereign_authorization',side_effect=AssertionError('metadata only')),redirect_stdout(out):
            result=cli.main(['stage-retry-plan',self.plan['plan_id'],'--state-db',str(self.path),'--reason','Review only'])
        child=json.loads(out.getvalue());self.assertEqual(result,0);self.assertEqual(child['authority'],'none')
        self.assertFalse(Path(child['staged_path']).exists());self.assertEqual(self.snapshot(self.plan['plan_id']),before)

    def test_failed_signed_parent_and_retry_have_distinct_consumed_grants(self):
        ledger=self.root/'signed-chain-ledger.db';env,responder,seen=self.signed_authorizer(ledger)
        parent_path=self.root/'parent-plan.json';parent_path.write_text(json.dumps(self.plan))
        self.source.write_bytes(b'drift')
        with patch.dict(os.environ,env,clear=True),patch.object(adapters.subprocess,'run',responder),redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            first=cli.main(['stage-execute','--plan',str(parent_path),'--state-db',str(self.path)])
            self.assertEqual(first,3)
            parent_before=self.snapshot(self.plan['plan_id'])
            self.assertEqual(parent_before['status'],'failed')
            self.assertEqual(parent_before['publication_state'],'not_attempted')
            self.source.write_bytes(b'synthetic source\n')
            child=self.retry();child_path=self.root/'retry-plan.json';child_path.write_text(json.dumps(child))
            second=cli.main(['stage-execute','--plan',str(child_path),'--state-db',str(self.path)])
        self.assertEqual(second,0);self.assertEqual(len(seen),2)
        with sqlite3.connect(ledger) as db:
            receipts=[json.loads(row[0]) for row in db.execute('SELECT receipt_json FROM consumed_grants')]
        self.assertEqual(len(receipts),2)
        self.assertEqual(len({r['grant_id'] for r in receipts}),2)
        self.assertEqual(len({r['authorization_nonce'] for r in receipts}),2)
        self.assertEqual(self.snapshot(self.plan['plan_id']),parent_before)
        self.assertIn(json.loads(parent_before['receipt_json']),receipts)
        self.assertIn(json.loads(self.snapshot(child['plan_id'])['receipt_json']),receipts)

    def test_linked_proposal_without_fresh_authorizer_cannot_execute(self):
        before=self.fail_before_publication();child=self.retry()
        path=self.root/'unauthorized-child.json';path.write_text(json.dumps(child))
        with patch.dict(os.environ,{},clear=True),redirect_stderr(io.StringIO()):
            result=cli.main(['stage-execute','--plan',str(path),'--state-db',str(self.path)])
        self.assertEqual(result,3);self.assertFalse(Path(child['staged_path']).exists())
        self.assertIsNone(self.snapshot(child['plan_id'])['receipt_json'])
        self.assertEqual(self.snapshot(self.plan['plan_id']),before)

    def test_parent_receipt_does_not_bind_new_retry_target(self):
        self.fail_before_publication();child=self.retry()
        with self.assertRaisesRegex(staging.StagingError,'does not bind'):
            staging.execute(self.db,child,self.receipt(self.plan))
        self.assertFalse(Path(child['staged_path']).exists())
        self.assertIsNone(self.snapshot(child['plan_id'])['receipt_json'])

    def test_retry_publication_process_crash_recovers_without_parent_mutation(self):
        parent_before=self.fail_before_publication();child=self.retry()
        plan_path=self.root/'child.json';plan_path.write_text(json.dumps(child))
        script = """import json,os,sys
from pathlib import Path
from sovereign_workbench import staging
plan=json.loads(Path(sys.argv[2]).read_text())
replace=Path.replace
def crash(path,target):
    result=replace(path,target)
    os._exit(73)
Path.replace=crash
with staging.connect(Path(sys.argv[1])) as db:
    staging.execute(db,plan,{'operation':'stage_copy','target':plan['staged_path'],'proposal_sha256':'0'*64})
"""
        env={'PYTHONPATH':str(Path(staging.__file__).resolve().parents[1])}
        if 'SystemRoot' in os.environ:env['SystemRoot']=os.environ['SystemRoot']
        result=subprocess.run([sys.executable,'-B','-c',script,str(self.path),str(plan_path)],
                              env=env,capture_output=True,timeout=20,check=False)
        self.assertEqual(result.returncode,73,result.stderr.decode())
        before=self.snapshot(child['plan_id'])
        self.assertEqual(before['status'],'staging');self.assertEqual(before['publication_state'],'publication_pending')
        self.assertIsNotNone(before['rollback_json'])
        staging.recover(self.db)
        self.assertEqual(self.snapshot(child['plan_id'])['status'],'staged')
        self.assertEqual(self.snapshot(self.plan['plan_id']),parent_before)
        self.assertEqual(Path(child['staged_path']).read_bytes(),self.source.read_bytes())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM staging_retry_links').fetchone()[0],1)

    def test_legacy_migration_preserves_columns_and_unknown_publication(self):
        legacy=self.root/'legacy.db'
        with sqlite3.connect(legacy) as db:
            db.executescript(staging.SCHEMA)
            db.execute("INSERT INTO staged_operations(plan_id,plan_json,status,receipt_json,rollback_json,error) VALUES(?,?,'failed',?,?,?)",
                       (self.plan['plan_id'],json.dumps(self.plan,sort_keys=True,separators=(',',':')),json.dumps(self.receipt(self.plan)),'retained-intent','retained-error'))
            before=db.execute('SELECT * FROM staged_operations').fetchone()
        with staging.connect(legacy) as db:
            cursor=db.execute('SELECT * FROM staged_operations');after=cursor.fetchone()
            self.assertEqual(after[:-1],before);self.assertIsNone(after[-1])
            child=staging.prepare_retry(db,self.plan['plan_id'],reason='Legacy review proposal only')
            with self.assertRaisesRegex(staging.StagingError,'uncertain or published'):staging.execute(db,child,self.receipt(child))
