"""Result metadata distinguishes native refusal without logging user/model content."""
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from flippy.codex_provider import CodexError

ROOT=Path(__file__).resolve().parents[1]


def controller(tools):
    path=ROOT/'flippy/daemon.py'
    tree=ast.parse(path.read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Flippy')
    event=Mock()
    from flippy.usage_guard import PlanLimitReached
    ns={'event':event,'os':os,'json':json,'__file__':str(path),'CodexError':CodexError,'PlanLimitReached':PlanLimitReached,
        'PROFILE':SimpleNamespace(demo=True,name='demo'),
        'updates':SimpleNamespace(current_version=lambda:'fixture'),
        'settings':SimpleNamespace(get=lambda *args:10),'act_memory':Mock(),'log':Mock(),'setup_gate':Mock(),'errors':__import__('flippy.errors').errors}
    for node in cls.body:
        if isinstance(node,ast.FunctionDef) and node.name in ('_action_done','command'):
            exec(compile(ast.Module(body=[node],type_ignores=[]),'<action results>','exec'),ns)
    f=type('Controller',(),{name:ns[name] for name in ('_action_done','command')})()
    f._owns=Mock(return_value=True)
    f.action_tools=tools
    f.action_future=object()
    f.ui=SimpleNamespace(action_card=SimpleNamespace(hide=Mock()))
    f.overlay=SimpleNamespace(show_text=Mock())
    f._fail=Mock(); f._schedule_fade=Mock(); f._open_setup_if=Mock(); f._act_trail=Mock(return_value=None)
    f._remember=Mock()
    f.busy=False; f.input_disabled=False
    f._save_history=Mock()  # History; tested in test_history_store
    return f,event


class TestActionResults(unittest.TestCase):
    def test_subscription_gate_is_visible_without_backend_details(self):
        tools=SimpleNamespace(failure=None,actions=0,cleanup_failed=False)
        f,_=controller(tools)
        safe = "Codex could not complete this request. Try starting a new chat."
        f._action_done(tools,SimpleNamespace(identity=20),None,CodexError(safe, "CODEX-FAILED"))
        self.assertEqual(f._fail.call_args.args[0], "Desktop task stopped. Codex couldn't answer. · CODEX-FAILED")
        self.assertEqual(f.last_action['completed_inputs'],0)
    def test_refusals_are_categorical_and_never_show_model_success(self):
        cases=(('The screen changed while approval was pending.','pixels_changed'),
               ('The foreground window or display changed.','target_changed'),
               ('Could not capture the screen. Check Screen Recording permission.','capture_failed'),
               ('Action declined. Do not retry.','declined'),
               ('Input release could not be confirmed.','cleanup_failed'),
               ('Allow Flippy in macOS Accessibility settings.','permission'),
               ('Desktop operation failed.','operation_failed'))
        for failure,code in cases:
            with self.subTest(code=code):
                tools=SimpleNamespace(failure=failure,actions=0,cleanup_failed=code=='cleanup_failed')
                f,event=controller(tools)
                f._action_done(tools,SimpleNamespace(identity=17),'private model claim of success',None)
                self.assertEqual(f.last_action['reason_code'],code)
                self.assertTrue(f.last_action['stopped'])
                self.assertEqual(f.last_action['completed_inputs'],0)
                self.assertEqual(set(f.last_action),{'request_id','completed_inputs','stopped','cleanup_failed','reason_code'})
                self.assertNotIn(failure,json.dumps(f.last_action))
                self.assertNotIn('private model',str(event.call_args))
                self.assertNotIn('private model',str(f._fail.call_args))
                f.overlay.show_text.assert_not_called()
                event.assert_called_once_with('action_result',request_id=17,count=0,outcome='stopped',reason_code=code)

    def test_reason_code_from_the_refusal_wins_over_its_wording(self):
        # "Let go of the keyboard..." used to read as a release failure ("restart Flippy") by word matching
        for failure, code in (("Let go of the keyboard and mouse while Flippy acts.", "input_held"),
                              ("Release physical keys and mouse buttons before Flippy acts.", "input_held")):
            with self.subTest(failure=failure):
                tools=SimpleNamespace(failure=failure,failure_code=code,actions=1,cleanup_failed=False)
                f,event=controller(tools)
                f._action_done(tools,SimpleNamespace(identity=20),None,None)
                self.assertEqual(f.last_action['reason_code'],'input_held')
                self.assertFalse(f.last_action['cleanup_failed'])
                self.assertNotIn('Restart Flippy',str(f._fail.call_args))

    def test_the_real_reason_shows_once(self):
        # Codex wraps a stopped task as "Desktop task stopped. Check the screen..." and the controller wrapped it again
        tools=SimpleNamespace(failure='Spotify quit. Start a new /act request.',failure_code=None,actions=2,cleanup_failed=False)
        f,event=controller(tools)
        f._action_done(tools,SimpleNamespace(identity=21),None,
                       CodexError('Desktop task stopped. Check the screen before continuing.'))
        shown=f._fail.call_args.args[0]
        self.assertEqual(shown.count('Desktop task stopped'),1)
        self.assertIn('Spotify quit',shown)
        self.assertTrue(shown.endswith('· ACT-STOPPED'))

    def test_success_counts_inputs_and_doctor_exposes_only_safe_receipt(self):
        tools=SimpleNamespace(failure=None,actions=2,cleanup_failed=False)
        f,event=controller(tools)
        f._action_done(tools,SimpleNamespace(identity=18),'private answer',None)
        receipt=json.loads(f.command('doctor'))['last_action']
        self.assertEqual(receipt,{'request_id':18,'completed_inputs':2,'stopped':False,'cleanup_failed':False,'reason_code':'none'})
        f.overlay.show_text.assert_called_once_with('private answer', thought=None, task=True)
        self.assertNotIn('private answer',json.dumps(receipt))
        self.assertNotIn('private answer',str(event.call_args))

    def test_backend_error_is_sanitized_without_raw_details(self):
        tools=SimpleNamespace(failure=None,actions=0,cleanup_failed=False)
        f,event=controller(tools)
        f._action_done(tools,SimpleNamespace(identity=19),None,RuntimeError('secret-path or private prompt'))
        self.assertEqual(f.last_action['reason_code'],'operation_failed')
        self.assertNotIn('secret-path',str(f._fail.call_args)+str(event.call_args))

    def test_stale_completion_does_not_replace_receipt_or_hide_current_card(self):
        tools=SimpleNamespace(failure='Action declined.',actions=0,cleanup_failed=False)
        f,event=controller(tools)
        f.last_action={'request_id':99}
        f._owns.return_value=False
        f._action_done(tools,SimpleNamespace(identity=1),'stale',None)
        self.assertEqual(f.last_action,{'request_id':99})
        f.ui.action_card.hide.assert_not_called()
        f._fail.assert_not_called()
        event.assert_not_called()
