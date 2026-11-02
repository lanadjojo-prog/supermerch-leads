"""Local message drafts only: this module has no Instagram send capability."""
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


def message(username, product):
    offer = {
        'socks': 'Zouden sportsokken in jullie eigen kleuren en met jullie logo interessant zijn?',
        'scarves': 'Zouden sjaals in jullie eigen kleuren en met jullie logo interessant zijn?',
        'open': 'Zijn jullie weleens op zoek naar merchandise in jullie eigen stijl?',
    }[product]
    return (f'Hi @{username}, ik ben Giovanni van SuperMerch. {offer} '
            'Als jullie dat leuk vinden, maak ik graag vrijblijvend een gratis ontwerpvoorstel. '
            'Zal ik een idee voor jullie uitwerken?')


def initial_draft(prospect, lead=None):
    product, reason = suggest(getattr(lead, 'industry', ''), getattr(lead, 'company_name', ''))
    return {'product': product, 'reason': reason, 'text': message(prospect.username, product)}


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
