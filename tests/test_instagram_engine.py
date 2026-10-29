from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from app.database import Base
from app.models import Campaign, Lead, InstagramProspect, InstagramAction
from app.services import instagram_engine as ig
from app.services.instagram_links import instagram_username

@pytest.fixture
def db():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Campaign(id=1,name='SuperMerch', niche='sport', region='NL', search_query='clubs'))
        session.add(Lead(id=1,campaign_id=1,company_name='Club',domain='example.org',website='https://example.org',score=80,status='ready_for_review'))
        session.add(InstagramProspect(username='club',lead_id=1,source_url='https://example.org'))
        session.commit()
        yield session

NOW = datetime(2026,10,27,12,tzinfo=timezone.utc)

def media(n=1):
    return {'id':str(n),'permalink':f'https://www.instagram.com/p/abc{n}/','timestamp':NOW.isoformat()}

class Client:
    def __init__(self,fail=False): self.calls=[]; self.fail=fail
    def verify(self): pass
    def media(self,username): return [media(n) for n in range(1,5)]
    def like(self,id):
        self.calls.append(id)
        if self.fail: raise ig.MetaError('Test API failure')

@pytest.mark.parametrize('url', ['https://instagram.com/p/abc','https://instagram.com/reels/abc','https://instagram.com.evil.org/club','https://evil.org/club','https://instagram.com/club/extra','https://instagram.com/accounts/'])
def test_reject_non_profiles(url):
    assert instagram_username(url) is None

def test_valid_profile():
    assert instagram_username('https://www.instagram.com/Club.NL/?igsh=x') == 'club.nl'

def test_two_likes_once(db):
    client=Client()
    assert ig.engage(db,client,NOW)['likes'] == 2
    assert ig.engage(db,client,NOW)['likes'] == 0
    assert len(client.calls) == 2

def test_limit_and_duplicate_reservations(db):
    for n in range(ig.DAILY_LIMIT):
        assert ig.reserve_like(db,'club',media(n),'2026-10-27')
    assert ig.reserve_like(db,'club',media(ig.DAILY_LIMIT),'2026-10-27') is None
    assert ig.reserve_like(db,'club',media(0),'2026-10-28') is None
    client=Client()
    assert ig.engage(db,client,NOW)['status']=='daily_limit'
    assert not client.calls
    assert db.get(InstagramProspect,1).status=='discovered'
    assert ig.reserve_like(db,'club',media(ig.DAILY_LIMIT),'2026-10-28')

def test_error_pauses_without_retry(db):
    client=Client(fail=True)
    assert ig.engage(db,client,NOW)['status']=='paused'
    assert ig.paused(db)
    assert ig.engage(db,client,NOW)['status']=='paused'
    assert len(client.calls)==1
    assert db.scalar(select(InstagramAction)).status=='unconfirmed'

def test_hour_claim(db):
    assert ig.claim(db,'hour-1','test') is not None
    assert ig.claim(db,'hour-1','test') is None

def test_discovery_records_source_once(db,monkeypatch):
    monkeypatch.setattr(ig,'crawl_website',lambda _:SimpleNamespace(pages=[('https://example.org','text')],instagram_profiles=[('newclub','https://example.org/contact')]))
    assert ig.discover(db)['accounts_found']==1
    assert ig.discover(db)['websites_scanned']==0
    assert db.scalar(select(InstagramProspect).where(InstagramProspect.username=='newclub')).source_url=='https://example.org/contact'

def test_stale_posts_skipped(db):
    client=Client()
    client.media=lambda username: [dict(media(), timestamp='2020-01-01T00:00:00+00:00')]
    assert ig.engage(db,client,NOW)['likes']==0
    assert not client.calls

def test_official_api_contract(monkeypatch):
    import httpx
    monkeypatch.setenv('INSTAGRAM_USER_ID','123')
    monkeypatch.setenv('INSTAGRAM_GRAPH_VERSION','v25.0')
    monkeypatch.setenv('INSTAGRAM_ACCESS_TOKEN','test-secret')
    seen=[]
    def respond(request):
        seen.append(request)
        if request.method=='POST':
            assert request.url.path=='/v25.0/123/likes'
            assert request.content==b'media_id=456'
            return httpx.Response(200,json={'success':True})
        return httpx.Response(200,json={'id':'123','username':'supermerch.nl'})
    client=ig.MetaClient.__new__(ig.MetaClient)
    client.user="123"
    client.client=httpx.Client(base_url='https://graph.facebook.com/v25.0/', transport=httpx.MockTransport(respond), headers={'Authorization':'Bearer test-secret'})
    client.verify()
    client.like('456')
    assert len(seen)==2
    assert all('test-secret' not in str(req.url) for req in seen)
    client.close()

def test_wrong_account_pauses(db):
    client=Client()
    def wrong(): raise ig.MetaError('Wrong account')
    client.verify=wrong
    assert ig.engage(db,client,NOW)['status']=='paused'
    assert not client.calls


def test_crawler_visits_social_page_before_news_budget_is_exhausted(monkeypatch):
    import httpx
    from app.services import crawler
    requests = []
    client_factory = httpx.Client
    def respond(request):
        requests.append(request.url.path)
        if request.url.path == '/social-media':
            body = '<html><a href="https://www.instagram.com/realclub/">Instagram</a></html>'
        else:
            body = '<html><p>Club website</p>' + ''.join(
                f'<a href="/{path}">{path}</a>' for path in
                ['nieuws/1','nieuws/2','nieuws/3','nieuws/4','contact','social-media']) + '</html>'
        return httpx.Response(200,headers={'content-type':'text/html'},text=body)
    monkeypatch.setattr(crawler, '_public_dns_host', lambda _: True)
    monkeypatch.setattr(crawler.httpx, 'Client', lambda **kwargs:
        client_factory(**kwargs, transport=httpx.MockTransport(respond)))
    result = crawler.crawl_website('https://club.example.org/')
    assert result.instagram_profiles == [('realclub','https://club.example.org/social-media')]
    assert requests[:3] == ['/', '/social-media', '/contact']


def test_finished_campaign_discovers_without_liking(db,monkeypatch):
    from app.services.pipeline import run_campaign
    monkeypatch.setenv('INSTAGRAM_CRAWLER_ENABLED','true')
    discovered=[]
    def discover(session):
        discovered.append(True)
        return {'websites_scanned':1,'accounts_found':1}
    monkeypatch.setattr(ig,'discover',discover)
    def forbidden(*args):
        raise AssertionError('Campaign completion must never place likes')
    monkeypatch.setattr(ig,'engage',forbidden)
    result=run_campaign(db,db.get(Campaign,1),companies=[])
    assert discovered==[True]
    assert result['instagram_discovery']['accounts_found']==1


def test_daily_run_never_repeats_when_meta_setting_changes(db,monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None): return NOW
    monkeypatch.setattr(ig,'datetime',FixedDatetime)
    monkeypatch.setenv('INSTAGRAM_CRAWLER_ENABLED','true')
    monkeypatch.setenv('INSTAGRAM_DAILY_NOT_BEFORE','')
    monkeypatch.setenv('INSTAGRAM_LIKES_ENABLED','false')
    monkeypatch.setattr(ig,'connection_ready',lambda: True)
    monkeypatch.setattr(ig,'SessionLocal',lambda: db)
    discovered=[]
    monkeypatch.setattr(ig,'discover',lambda session: (
        discovered.append(True) or {'websites_scanned':0,'accounts_found':0}))
    client=Client()
    client.close=lambda: None
    monkeypatch.setattr(ig,'MetaClient',lambda: client)
    ig.run_background()
    assert not client.calls
    monkeypatch.setenv('INSTAGRAM_LIKES_ENABLED','true')
    ig.run_background()
    ig.run_background()
    assert not client.calls
    assert len(discovered)==1
    from app.models import ChatCommand
    assert db.scalar(select(func.count(ChatCommand.id)).where(
        ChatCommand.command_id=='instagram-hour-2026-10-27-13')) == 1


def test_mailing_csv_import_is_deduplicated_and_no_email_is_stored(db):
    from app.models import ChatCommand
    from app.services.instagram_priority import import_mailing_csv, priority_counts
    payload = ("Bedrijfsnaam;E-mail;Website\n"
               "Nieuwe club;directie@newclub.nl;https://newclub.nl\n"
               "Nieuwe club;contact@newclub.nl;https://newclub.nl\n"
               "Privé persoon;persoon@gmail.com;\n").encode()
    first = import_mailing_csv(db, payload)
    assert first == {'toegevoegd':1,'al_bekend':0,'overgeslagen':1,'totaal_uniek':1}
    assert import_mailing_csv(db, payload)['al_bekend'] == 1
    queue = db.scalars(select(ChatCommand).where(ChatCommand.action=='instagram_priority')).all()
    assert len(queue) == 1 and queue[0].status == 'queued'
    assert 'directie@' not in queue[0].result
    assert 'contact@' not in queue[0].result
    assert priority_counts(db)['wacht'] == 1


def test_priority_mailing_list_scans_before_regular_leads(db, monkeypatch):
    from app.models import ChatCommand
    from app.services import instagram_priority as priority
    priority.import_mailing_csv(db, b'bedrijfsnaam,website\nMijn club,https://newclub.nl\n')
    visited = []
    def crawl(url):
        visited.append(url)
        return SimpleNamespace(
            pages=[(url, 'Organisatie')],
            instagram_profiles=[('mijnclub', url + '/contact')])
    monkeypatch.setattr(priority, 'crawl_website', crawl)
    monkeypatch.setattr(ig, 'crawl_website', lambda url: (_ for _ in ()).throw(
        AssertionError("Regular leads must wait for the imported list")))
    monkeypatch.setattr(ig, 'DAILY_ACCOUNTS_LIMIT', 2)
    assert ig.discover(db) == {
        'websites_scanned': 1, 'accounts_found': 1, 'priority_websites_scanned': 1}
    assert visited == ['https://newclub.nl']
    assert db.scalar(select(InstagramProspect).where(
        InstagramProspect.username=='mijnclub')) is not None
    assert db.scalar(select(ChatCommand).where(
        ChatCommand.action=='instagram_priority')).status == 'matched'
    assert ig.discover(db)['websites_scanned'] == 0


def test_engagement_prefers_imported_account_even_if_newer(db, monkeypatch):
    from app.models import ChatCommand
    import json
    from app.services import instagram_priority as priority
    from app.models import Lead
    priority.import_mailing_csv(db, b'company,domain\nNieuwe club,newclub.nl\n')
    target = db.scalar(select(ChatCommand).where(ChatCommand.action=='instagram_priority'))
    target.status = 'matched'
    value = json.loads(target.result)
    value['username'] = 'newclub'
    target.result = json.dumps(value)
    db.add(Lead(id=2, campaign_id=1, company_name='Newclub',
                domain='newclub.nl', website='https://newclub.nl',
                score=0, status='instagram_priority'))
    db.add(InstagramProspect(username='newclub', lead_id=2, source_url='https://newclub.nl'))
    db.commit()
    monkeypatch.setattr(ig, 'DAILY_ACCOUNTS_LIMIT', 1)
    client = Client()
    names = []
    def retrieve(username):
        names.append(username)
        return [media(100), media(101)]
    client.media = retrieve
    assert ig.engage(db, client, NOW)['likes'] == 2
    assert names == ['newclub']
