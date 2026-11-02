import importlib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from test_instagram_message_drafts import PKG

identity = importlib.import_module(PKG + '.services.supermerch_identity')
config = types.ModuleType(PKG + '.config')
config.settings = types.SimpleNamespace(session_secret='test', zoho_mail_base='https://example.invalid',
    zoho_client_id='', zoho_client_secret='', free_mode=True, openai_api_key='')
sys.modules[PKG + '.config'] = config
zoho = importlib.import_module(PKG + '.services.zoho_mail')


class SenderIdentityTests(unittest.TestCase):
    def test_old_intro_and_signature_are_corrected(self):
        for text in ('Ik ben Giovanni van SuperMerch.\n\nGroet,\nGiovanni\nSuperMerch',
                     r'Ik ben Giovanni van SuperMerch.\n\nGroet,\nGiovanni\nSuperMerch',
                     'GIOVANNI DJOJOWARDI | SuperMerch'):
            value = identity.normalize_sender_identity(text)
            self.assertNotIn('giovanni', value.lower())
            self.assertIn('Chris', value)
            self.assertEqual(value, identity.normalize_sender_identity(value))

    def test_recipient_name_is_not_silently_rewritten(self):
        with self.assertRaises(ValueError):
            identity.normalize_sender_identity('Beste Giovanni, dit is ons voorstel.')
        self.assertEqual(identity.normalize_sender_identity('Hoi Chris van SuperMerch!'),
                         'Hoi Chris van SuperMerch!')

    def test_zoho_draft_payload_enforces_identity_without_sending(self):
        db = MagicMock()
        db.scalar.return_value = types.SimpleNamespace(email_address='info@supermerch.nl',
            account_id='test', refresh_token_encrypted='encrypted')
        response = MagicMock(status_code=200)
        response.json.return_value = {'status': {'code': 200}, 'data': {'messageId': 'test'}}
        with patch.object(zoho, '_decrypt', return_value='dummy'), \
             patch.object(zoho, 'refresh_access_token', return_value='dummy'), \
             patch.object(zoho.httpx, 'Client') as client:
            client.return_value.__enter__.return_value.post.return_value = response
            zoho.save_draft(db, to_address='test@example.invalid', subject='Een idee',
                content='Ik ben Giovanni van SuperMerch.\n\nGiovanni')
            body = client.return_value.__enter__.return_value.post.call_args.kwargs['json']
            self.assertEqual(body['mode'], 'draft')
            self.assertIn('Chris', body['content'])
            self.assertNotIn('Giovanni', body['content'])
            self.assertIn('Chris | Supermerch', body['content'])

    def test_ambiguous_copy_is_blocked_before_api_request(self):
        db = MagicMock()
        db.scalar.return_value = types.SimpleNamespace(email_address='info@supermerch.nl', account_id='test')
        with patch.object(zoho.httpx, 'Client') as client:
            with self.assertRaises(ValueError):
                zoho.save_draft(db, to_address='test@example.invalid', subject='Idee',
                    content='Beste Giovanni, dit is een voorstel.')
            client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
