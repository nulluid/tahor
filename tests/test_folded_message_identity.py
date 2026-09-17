"""Malformed Message-ID text is comparison data, never an IMAP command."""
import unittest
from unittest.mock import Mock, patch
import message_reviews


class FoldedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.client=Mock()
        self.client.select.return_value=('OK',[])
        self.client.response.return_value=('UIDVALIDITY',[b'42'])
        self.identifier='<invoice=first\r\n &\r\n second@example.com>'
        self.context=dict(mailbox='INBOX',uid='7',uidvalidity='42',message_id=self.identifier)
        self.raw=('Message-ID: '+self.identifier+'\r\nSubject: Invoice\r\nFrom: billing@example.com\r\n\r\n').encode()
        self.meta=b'1 (UID 7 INTERNALDATE "01-Sep-2026 12:30:00 +0000")'
        self.client.uid.return_value=('OK',[(self.meta,self.raw)])
        self.patcher=patch.object(message_reviews.fetch_batch,'connect',return_value=self.client)
        self.connect=self.patcher.start();self.addCleanup(self.patcher.stop)

    def test_folded_real_header_is_verified_by_original_uid_without_search(self):
        details=message_reviews.locate(self.context)
        self.assertEqual(details['uid'],'7')
        self.assertEqual(self.client.uid.call_count,1)
        self.assertEqual(self.client.uid.call_args.args[0],'FETCH')
        self.assertNotIn(self.identifier,str(self.client.uid.call_args))
        self.assertIn('BODY.PEEK',self.client.uid.call_args.args[-1])

    def test_recreated_folder_never_searches_or_uses_recycled_uid(self):
        self.client.response.return_value=('UIDVALIDITY',[b'43'])
        with self.assertRaises(ValueError):message_reviews.locate(self.context)
        self.client.uid.assert_not_called()

    def test_missing_or_changed_original_never_looks_for_another_copy(self):
        for response in [('OK',[None]),('OK',[(self.meta,self.raw.replace(b'first',b'other'))])]:
            with self.subTest(response=response[1][0] is None):
                self.client.uid.reset_mock();self.client.uid.return_value=response
                with self.assertRaises(ValueError):message_reviews.locate(self.context)
                self.assertEqual(self.client.uid.call_count,1)
                self.assertEqual(self.client.uid.call_args.args[0],'FETCH')
                self.client.list.assert_not_called()

    def test_legacy_or_invalid_shortcuts_reject_before_connecting(self):
        for changes in ({'uid':None},{'uid':'7\r\nSEARCH ALL'},{'uidvalidity':'old'},{'uid':'4294967296'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                message_reviews.locate(dict(self.context,**changes))
        self.connect.assert_not_called()

    def test_direct_folder_search_also_refuses_control_text(self):
        with self.assertRaises(ValueError):
            message_reviews._folder_matches(self.client,'INBOX',self.identifier)
        self.client.uid.assert_not_called()
