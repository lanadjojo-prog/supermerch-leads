"""Local message drafts only: this module has no Instagram send capability."""
import hashlib
import json
import re
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from ..models import ChatCommand, InstagramProspect, Lead

PRODUCTS = {'socks': 'Sportsokken', 'scarves': 'Sjaals', 'open': 'Nog kiezen'}


def suggest(industry='', company='', niche=''):
    context = ' '.join((industry or '', company or '', niche or '')).lower()
    if re.search(r'supporter|fanclub|carnaval|studenten|studievereniging', context):
        return 'scarves', 'Mogelijke match voor clubidentiteit en supporters; controleer de bron.'
    if re.search(r'sport|fitness|gym|crossfit|hyrox|hardloop|running|voetbal|hockey|padel|tennis', context):
        return 'socks', 'Mogelijke match voor sporters en teams; controleer de bron.'
    return 'open', 'Te weinig informatie voor een gerichte productkeuze.'


def variant_for(username):
    # Stable assignment prevents repeat visits from switching a prospect's text.
    return 'ABC'[int(hashlib.sha256(username.lower().encode()).hexdigest()[:8], 16) % 3]


def message(username, product, variant=None):
    variant = variant or variant_for(username)
    item = {'socks': 'sportsokken', 'scarves': 'sjaals', 'open': 'kleding of merchandise'}[product]
    idea = f'{item} in jullie eigen design en kleuren'
    maybe = f'Misschien zijn {idea} een leuk idee voor jullie.' if product != 'open' else 'Misschien is kleding of merchandise in jullie eigen design en kleuren een leuk idee voor jullie.'
    versions = {
        'A': f'Hoi! Chris van SuperMerch hier. Ik dacht aan {item} in jullie eigen design en kleuren. Zou dat iets voor jullie zijn? Als jullie willen, maak ik vrijblijvend een gratis ontwerpvoorstel.',
        'B': f'Hoi! Ik ben Chris van SuperMerch. Hebben jullie weleens gedacht aan {item} helemaal in jullie eigen design en kleuren? Ik kan vrijblijvend een gratis ontwerpvoorstel maken. Lijkt het jullie leuk om eens te zien?',
        'C': f'Hoi! Ik ben Chris van SuperMerch. {maybe} Zal ik vrijblijvend een gratis ontwerpvoorstel maken?',
    }
    text = versions[variant]
    # User-supplied positioning; no invented quantities or price guarantees.
    detail = 'Dat kan ook in kleine oplages, tegen scherpe prijzen. '
    for opening in ('Als jullie willen,', 'Ik kan vrijblijvend', 'Zal ik vrijblijvend'):
        if opening in text:
            return text.replace(opening, detail + opening, 1)
    return text


def initial_draft(prospect, lead=None):
    product, reason = suggest(getattr(lead, 'industry', ''), getattr(lead, 'company_name', ''))
    variant = variant_for(prospect.username)
    return {'product': product, 'reason': reason, 'variant': variant, 'version': 2,
            'outcome': 'unknown', 'edited': False,
            'text': message(prospect.username, product, variant)}


def ensure_drafts(db):
    """Backfill existing accounts and include new discoveries; never overwrite edits."""
    rows = db.execute(select(InstagramProspect, Lead).outerjoin(
        Lead, InstagramProspect.lead_id == Lead.id).where(~select(ChatCommand.id).where(
            ChatCommand.command_id == 'instagram-draft-' + InstagramProspect.username
        ).exists())).all()
    for prospect, lead in rows:
        draft = ChatCommand(command_id=f'instagram-draft-{prospect.username}',
            action='instagram_message_draft', status='draft',
            result=json.dumps(initial_draft(prospect, lead), ensure_ascii=False))
        try:
            with db.begin_nested():
                db.add(draft)
                db.flush()
        except IntegrityError:
            pass
    db.commit()


def draft_cards(db, prospects):
    usernames = [p.username for p in prospects]
    saved = db.scalars(select(ChatCommand).where(ChatCommand.command_id.in_(
        [f'instagram-draft-{name}' for name in usernames]))).all() if usernames else []
    by_name = {row.command_id: row for row in saved}
    cards = {}
    for prospect in prospects:
        row = by_name.get(f'instagram-draft-{prospect.username}')
        lead = db.get(Lead, prospect.lead_id) if prospect.lead_id else None
        try:
            data = json.loads(row.result) if row else initial_draft(prospect, lead)
            if not isinstance(data, dict):
                raise ValueError('Invalid draft')
        except (ValueError, TypeError):
            data = initial_draft(prospect, lead)
        cards[prospect.id] = dict(data, status=row.status if row else 'draft')
    return cards


def experiment_summary(db):
    """Only manually recorded sends/replies count; copied drafts are not sends."""
    groups = {}
    for row in db.scalars(select(ChatCommand).where(
            ChatCommand.action == 'instagram_message_draft')).all():
        try:
            data = json.loads(row.result or '{}')
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get('version') != 2 or not data.get('sent_at'):
            continue
        if data.get('edited') or data.get('variant') not in ('A', 'B', 'C'):
            continue
        key = (data.get('product', 'open'), data['variant'])
        group = groups.setdefault(key, {'product': PRODUCTS.get(key[0], key[0]),
            'variant': key[1], 'sent': 0, 'replied': 0, 'interested': 0})
        group['sent'] += 1
        group['replied'] += data.get('outcome') in ('replied', 'interested', 'not_interested')
        group['interested'] += data.get('outcome') == 'interested'
    return sorted(groups.values(), key=lambda r: (r['product'], r['variant']))
