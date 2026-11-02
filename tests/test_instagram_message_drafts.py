import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import DeclarativeBase, Session
from jinja2 import DictLoader, Environment, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
# Load the pure draft feature without starting the application's schedulers.
PKG = '_instagram_draft_test_app'
package = types.ModuleType(PKG)
package.__path__ = [str(ROOT / 'app')]
sys.modules[PKG] = package
db_module = types.ModuleType(PKG + '.database')
class Base(DeclarativeBase):
    pass
db_module.Base = Base
sys.modules[PKG + '.database'] = db_module
models = __import__(PKG + '.models', fromlist=['models'])
drafts = __import__(PKG + '.services.instagram_drafts', fromlist=['instagram_drafts'])


class MessageDraftTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.prospect = models.InstagramProspect(username='testclub', lead_id=0, source_url='https://example.org')
        self.db.add(self.prospect)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_product_matches_and_uncertainty(self):
        self.assertEqual(drafts.suggest('fitness')[0], 'socks')
        self.assertEqual(drafts.suggest('voetbal supportersclub')[0], 'scarves')
        self.assertEqual(drafts.suggest('bakkerij')[0], 'open')
        self.assertEqual(drafts.suggest()[0], 'open')

    def test_backfill_is_idempotent_and_preserves_edit_and_sent_status(self):
        drafts.ensure_drafts(self.db)
        row = self.db.scalar(select(models.ChatCommand))
        self.assertEqual(row.status, 'draft')
        self.assertEqual(json.loads(row.result)['product'], 'open')
        row.result = json.dumps({'product': 'scarves', 'text': 'Mijn eigen tekst'})
        row.status = 'manually_sent'
        self.db.commit()
        drafts.ensure_drafts(self.db)
        self.assertEqual(len(self.db.scalars(select(models.ChatCommand)).all()), 1)
        card = drafts.draft_cards(self.db, [self.prospect])[self.prospect.id]
        self.assertEqual(card['text'], 'Mijn eigen tekst')
        self.assertEqual(card['status'], 'manually_sent')
        new = models.InstagramProspect(username='newclub', lead_id=0, source_url='https://example.org')
        self.db.add(new)
        self.db.commit()
        drafts.ensure_drafts(self.db)
        self.assertEqual(len(self.db.scalars(select(models.ChatCommand)).all()), 2)

    def test_render_escapes_message_and_has_manual_controls(self):
        drafts.ensure_drafts(self.db)
        row = self.db.scalar(select(models.ChatCommand))
        row.result = json.dumps({'product': 'open', 'reason': 'Controleren',
            'text': '</textarea><script>alert(1)</script>'})
        self.db.commit()
        env = Environment(loader=DictLoader({'base.html': '{% block content %}{% endblock %}',
            'instagram.html': (ROOT / 'app/templates/instagram.html').read_text()}),
            autoescape=select_autoescape(['html']))
        html = env.get_template('instagram.html').render(
            request=types.SimpleNamespace(query_params={}),
            prospects=[self.prospect], message_drafts=drafts.draft_cards(self.db, [self.prospect]),
            draft_products=drafts.PRODUCTS, priority_counts={})
        self.assertIn('&lt;/textarea&gt;&lt;script&gt;', html)
        self.assertNotIn('</textarea><script>alert(1)', html)
        self.assertIn('Kopieer bericht', html)
        self.assertIn('Markeer als zelf verstuurd', html)
        self.assertIn('/instagram/draft/', html)


if __name__ == '__main__':
    unittest.main()
