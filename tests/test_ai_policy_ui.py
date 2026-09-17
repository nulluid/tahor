import json
import re
from unittest.mock import patch
from test_web_security import AppTestCase

class AIPolicyUITests(AppTestCase):
    def post(self, **data):
        return self.client.post('/settings',data=dict(data,csrf_token=self.token()))

    def test_each_task_has_four_real_roundtrip_policies_without_changing_others(self):
        settings=self.module.mailbox_settings
        original=settings.load_settings()
        self.addCleanup(settings.save_settings,original)
        with patch.dict('os.environ',{'TAHOR_CLASSIFY_FREE_ENABLED':'1'}):
            for task in ('classification','reply','rule'):
                for policy in ('paid_only','paid','auto','free'):
                    with self.subTest(task=task,policy=policy):
                        untouched={other:settings.get_ai_policy(other) for other in ('classification','reply','rule') if other!=task}
                        fields={'ai_task':task,'ai_policy':policy}
                        if task!='classification':fields.update(paid_model='grok-4.6',free_model='ling-free')
                        self.assertEqual(self.post(**fields).status_code,302)
                        self.assertEqual(settings.get_ai_policy(task),policy)
                        page=self.client.get('/settings').get_data(as_text=True)
                        form=re.search(r'<form[^>]+data-ai-task="'+task+r'".*?</form>',page,re.S).group()
                        self.assertIn(f'name="ai_policy" value="{policy}" checked',form)
                        self.assertEqual(len(re.findall('name="ai_policy"',form)),4)
                        for other,before in untouched.items():self.assertEqual(settings.get_ai_policy(other),before)

    def test_bad_policies_and_swapped_paid_free_models_do_not_save(self):
        settings=self.module.mailbox_settings
        before=settings.load_settings()
        for fields in ({'ai_task':'bogus','ai_policy':'free'}, {'ai_task':'reply','ai_policy':'bogus'}, {'ai_task':'reply','ai_policy':'free','paid_model':'ling-free','free_model':'grok-4.6'}, {'ai_task':'rule','ai_policy':'free','paid_model':'grok-4.6','free_model':'grok-4.6'}):
            self.assertEqual(self.post(**fields).status_code,400)
            self.assertEqual(settings.load_settings(),before)

    def test_free_writing_policy_does_not_reenable_disabled_task(self):
        settings=self.module.mailbox_settings
        original=settings.load_settings();self.addCleanup(settings.save_settings,original)
        for task in ('reply','rule'):
            self.assertEqual(self.post(ai_task=task,ai_policy='free',paid_model='none',free_model='ling-free').status_code,302)
            self.assertFalse(settings.is_ai_enabled(task))
            self.assertEqual(settings.get_ai_policy(task),'free')

    def test_quality_disclosures_and_no_one_hour_promise(self):
        page=self.client.get('/settings').get_data(as_text=True)
        self.assertIn('wrongly trashed five messages',page)
        self.assertIn('wrong folder once',page)
        self.assertIn('unsupported promises',page)
        self.assertIn('four hours',page)
        self.assertNotIn('under an hour',page)
        self.assertNotIn('one-hour target',page)

    def test_administrator_disabled_free_classifier_cannot_be_selected(self):
        with patch.dict('os.environ',{'TAHOR_CLASSIFY_FREE_ENABLED':'0'}):
            self.assertEqual(self.post(ai_task='classification',ai_policy='free').status_code,400)
            self.assertEqual(self.post(ai_task='classification',ai_policy='auto').status_code,400)
