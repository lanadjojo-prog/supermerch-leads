"""Mailinglijst-prioriteit voor Instagram-onderzoek.

Bewaar uitsluitend bedrijfsnaam, zakelijk domein en website; geen e-mailadressen.
Geen automatische e-mails, DMs of likes bij import/onderzoek.
We gebruiken bestaande ChatCommand-opslag zodat Neon geen destructieve migratie nodig heeft.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from ..models import Campaign, ChatCommand, InstagramProspect, Lead
from .crawler import crawl_website
from .discovery import normalize_domain
from .openai_discovery import public_url

FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.nl",
    "live.com", "yahoo.com", "yahoo.nl", "icloud.com", "me.com", "aol.com",
    "proton.me", "protonmail.com", "msn.com", "ziggo.nl", "kpnmail.nl",
}
EMAIL_COLUMNS = ("email", "e-mail", "e-mailadres", "mail", "email address", "email_address")
SITE_COLUMNS = ("website", "website_url", "url", "site", "domein", "domain", "webadres")
NAME_COLUMNS = ("bedrijfsnaam", "bedrijf", "company", "company_name", "organisatie", "name", "naam")
MAX_BYTES = 2_000_000
MAX_ROWS = 5000


def _pick(row, keys):
    return next((str(row[k]).strip() for k in keys if row.get(k)), "")


def _normalize_record(row):
    mail = _pick(row, EMAIL_COLUMNS).lower()
    website_text = _pick(row, SITE_COLUMNS)
    domain = ""
    website = ""
    if website_text:
        candidate = website_text if "://" in website_text else "https://" + website_text
        website = public_url(candidate)
        domain = normalize_domain(website)
    if not domain and "@" in mail:
        mail_domain = mail.rsplit("@", 1)[1].strip()
        if mail_domain not in FREE_EMAIL_DOMAINS:
            website = public_url("https://" + mail_domain)
            domain = normalize_domain(website)
    if not domain:
        return None
    name = _pick(row, NAME_COLUMNS)[:220] or domain
    return {"domain": domain[:220], "company_name": name, "website": website[:500]}


def parse_mailing_csv(content: bytes):
    if len(content) > MAX_BYTES:
        raise ValueError("Bestand is te groot (maximaal 2 MB).")
    try:
        data = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Gebruik een CSV-bestand in UTF-8.") from exc
    if not data.strip():
        raise ValueError("CSV is leeg.")
    sample = data[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(data), delimiter=delimiter)
    fields = {str(k).strip().lower() for k in (reader.fieldnames or [])}
    if not fields.intersection(set(EMAIL_COLUMNS) | set(SITE_COLUMNS)):
        raise ValueError("CSV moet een kolom e-mail, website of domein bevatten.")
    records = {}
    invalid = 0
    for index, original in enumerate(reader, 1):
        if index > MAX_ROWS:
            raise ValueError("Maximaal 5.000 regels per import.")
        row = {str(k).strip().lower(): v for k, v in original.items() if k}
        target = _normalize_record(row)
        if target:
            records.setdefault(target["domain"], target)
        else:
            invalid += 1
    return list(records.values()), invalid


def _queue_id(domain):
    return "instagram-priority-" + hashlib.sha256(domain.encode("utf-8")).hexdigest()[:32]


def import_mailing_csv(db, content: bytes):
    targets, invalid = parse_mailing_csv(content)
    added = existing = 0
    for target in targets:
        key = _queue_id(target["domain"])
        row = db.scalar(select(ChatCommand).where(ChatCommand.command_id == key))
        if row is not None:
            existing += 1
            # Bij een nieuwe import mag een eerdere mislukte/no-match scan opnieuw.
            if row.status in {"not_found", "failed"}:
                row.status = "queued"
            if row.status == "matched":
                try:
                    previous = json.loads(row.result or "{}")
                    target["username"] = previous.get("username", "")
                    target["source_url"] = previous.get("source_url", "")
                except (ValueError, TypeError):
                    pass
            row.result = json.dumps(target, ensure_ascii=False)
            continue
        db.add(ChatCommand(command_id=key, action="instagram_priority",
                           status="queued", result=json.dumps(target, ensure_ascii=False)))
        try:
            db.flush()
            added += 1
        except IntegrityError:
            db.rollback()
            existing += 1
    db.commit()
    return {"toegevoegd": added, "al_bekend": existing, "overgeslagen": invalid,
            "totaal_uniek": len(targets)}


def priority_counts(db):
    # Aggregate in SQL: loading 21k+ queue records on every dashboard render
    # needlessly blocks the service and grows memory usage.
    counts = dict(db.execute(
        select(ChatCommand.status, func.count(ChatCommand.id)).where(
            ChatCommand.action == "instagram_priority"
        ).group_by(ChatCommand.status)
    ).all())
    return {
        "totaal": sum(counts.values()),
        "wacht": counts.get("queued", 0),
        "gevonden": counts.get("matched", 0),
        "niet_gevonden": counts.get("not_found", 0),
        "mislukt": counts.get("failed", 0),
    }


def priority_usernames(db):
    handles = set()
    for row in db.scalars(select(ChatCommand).where(
            ChatCommand.action == "instagram_priority", ChatCommand.status == "matched")):
        try:
            username = json.loads(row.result or "{}").get("username", "")
            if username:
                handles.add(username)
        except (ValueError, TypeError):
            continue
    return handles


def _find_or_create_lead(db, target):
    domain = target["domain"]
    lead = db.scalar(select(Lead).where(Lead.domain == domain))
    if lead:
        return lead
    campaign = db.scalar(select(Campaign).where(Campaign.name == "Instagram mailinglijst import"))
    if campaign is None:
        campaign = Campaign(name="Instagram mailinglijst import", niche="Instagram",
                            region="Nederland", search_query="Eigen mailinglijst",
                            status="imported", target_count=0)
        db.add(campaign)
        db.flush()
    lead = Lead(campaign_id=campaign.id, company_name=target["company_name"],
                domain=domain, website=target["website"], source="instagram_priority_csv",
                status="instagram_priority", score=0)
    db.add(lead)
    db.flush()
    return lead


def discover_priority(db, budget, claim):
    """Verwerk eerst geïmporteerde bedrijven; pas daarna de gewone leadzoeker."""
    checked = found = 0
    if budget <= 0:
        return {"websites_scanned": 0, "accounts_found": 0}
    rows = db.scalars(select(ChatCommand).where(
        ChatCommand.action == "instagram_priority",
        ChatCommand.status == "queued").order_by(ChatCommand.id).limit(budget)).all()
    for row in rows:
        day = datetime.utcnow().date().isoformat()
        if not claim(db, f"instagram-priority-scan-{row.id}-{day}", "instagram_priority_scan"):
            continue
        checked += 1
        try:
            target = json.loads(row.result)
            result = crawl_website(target["website"])
            if not result.pages:
                row.status = "failed"
            elif not result.instagram_profiles:
                row.status = "not_found"
            else:
                username, source = next(((u, s) for u, s in result.instagram_profiles
                                         if u != "supermerch.nl"), ("", ""))
                if not username:
                    row.status = "not_found"
                else:
                    prospect = db.scalar(select(InstagramProspect).where(
                        InstagramProspect.username == username))
                    if prospect is None:
                        lead = _find_or_create_lead(db, target)
                        db.add(InstagramProspect(username=username, lead_id=lead.id,
                                                 source_url=source))
                    target["username"] = username
                    target["source_url"] = source
                    row.result = json.dumps(target, ensure_ascii=False)
                    row.status = "matched"
                    found += 1
            row.finished_at = datetime.utcnow()
            db.commit()
        except Exception:
            db.rollback()
            # Do not silently discard a failed import record.
            row = db.get(ChatCommand, row.id)
            if row:
                row.status = "failed"
                row.finished_at = datetime.utcnow()
                db.commit()
    return {"websites_scanned": checked, "accounts_found": found}
