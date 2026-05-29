# ==============================================================================
# SATYA — AGGREGATOR (Repo 6)
#
# Reads from Classified Sheet + entities.json + promises.json
# Builds static JSON files for the frontend.
#
# Auto-expansion logic — generates JSON only when enough data exists.
#
# Output JSONs:
#   - india_overview.json          (home page)
#   - party_<name>.json            (per party with sufficient mentions)
#   - state_<name>.json            (per state with >= MIN_STATE_ARTICLES)
#   - minister_<name>.json         (per minister with >= MIN_MINISTER_ARTICLES)
#   - topic_<name>.json            (per topic with >= MIN_TOPIC_ARTICLES)
#   - promises_summary.json        (promise tracker overview)
#   - manifest.json                (index of all available JSONs)
# ==============================================================================

import os
import json
import time
import logging
import re
import requests
from datetime import datetime, timedelta
from collections import defaultdict, Counter
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ==============================================================================
# --- CONFIGURATION ---
# ==============================================================================
CLASSIFIED_SHEET_NAME = 'Satya Classified'
CLASSIFIED_WORKSHEET_NAME = 'Sheet1'

ENTITIES_JSON_URL = os.environ.get('ENTITIES_JSON_URL', '')
PROMISES_JSON_URL = os.environ.get('PROMISES_JSON_URL', '')

# Output directory
OUTPUT_DIR = './api'

# Auto-expansion thresholds
MIN_STATE_ARTICLES = 15        # Min articles to generate state JSON
MIN_MINISTER_ARTICLES = 10     # Min articles to generate minister JSON
MIN_PARTY_ARTICLES = 15        # Min articles to generate party JSON
MIN_TOPIC_ARTICLES = 30        # Min articles to generate topic JSON
MIN_CITY_ARTICLES = 15         # Min articles to generate city JSON

# Time windows
RECENT_DAYS = 30               # For "recent" feeds
TOP_STORIES_DAYS = 7           # For home page top stories
MAX_ARTICLES_PER_JSON = 200    # Cap articles per JSON file

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ==============================================================================
# --- DATA LOADING ---
# ==============================================================================

def connect_to_sheets():
    logging.info("Connecting to Google Sheets...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    gcp_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not gcp_json:
        raise ValueError("GCP_SERVICE_ACCOUNT_JSON missing!")
    creds_dict = json.loads(gcp_json)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = client.open(CLASSIFIED_SHEET_NAME).worksheet(CLASSIFIED_WORKSHEET_NAME)
    return sheet

def fetch_articles(sheet):
    logging.info("Fetching classified articles...")
    raw_data = sheet.col_values(1)
    articles = []
    for cell in raw_data:
        if not cell:
            continue
        try:
            article = json.loads(cell)
            scraped_raw = article.get('scraped_at', '')
            try:
                article['scraped_dt'] = datetime.strptime(
                    str(scraped_raw).split('.')[0], "%Y-%m-%d %H:%M:%S"
                )
            except:
                article['scraped_dt'] = datetime.now() - timedelta(days=365)
            articles.append(article)
        except json.JSONDecodeError:
            continue
    logging.info(f"Fetched {len(articles)} articles.")
    return articles

def load_json_from_url(url, fallback_path=None):
    if url:
        try:
            url = url.strip()  # remove any trailing whitespace/newlines from secrets
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.warning(f"Failed to fetch from {url}: {e}")
    if fallback_path and os.path.exists(fallback_path):
        with open(fallback_path, 'r') as f:
            return json.load(f)
    return None

# ==============================================================================
# --- ENRICHMENT — Implicit linking via entities.json ---
# ==============================================================================

def verify_surname_context(match, text_lower, canonical_name):
    """
    Scans a 200-character window surrounding a matched generic surname
    to verify if it refers to the intended canonical politician.
    """
    alias_matched = match.group(0).lower()

    # If the match already contains a space, it represents a full name and is inherently unambiguous
    if len(alias_matched.split()) > 1:
        return True

    # Define context window boundaries
    start_idx = max(0, match.start() - 100)
    end_idx = min(len(text_lower), match.end() + 100)
    context_window = text_lower[start_idx:end_idx]

    # Keyword lists for common regional surnames in Indian politics
    disambiguation_rules = {
        "Narendra Modi": {"pm", "prime minister", "narendra", "varanasi", "namo", "central government", "modi cabinet"},
        "Amit Shah": {"home minister", "amit", "gandhinagar", "cooperation", "shah cabinet"},
        "Rahul Gandhi": {"congress", "rahul", "wayanad", "rae bareli", "opposition leader", "gandhi scion"},
        "Mamata Banerjee": {"cm", "chief minister", "didi", "mamata", "west bengal", "trinamool", "tmc"},
        "Arvind Kejriwal": {"aap", "arvind", "delhi", "chief minister", "cm", "convenor", "tihar"},
        "Akhilesh Yadav": {"sp", "samajwadi", "akhilesh", "up", "uttar pradesh", "karhal"},
        "Tejashwi Yadav": {"rjd", "tejashwi", "bihar", "patna", "rashtriya janata"}
    }

    keywords = disambiguation_rules.get(canonical_name)
    if not keywords:
        return True  # Default to true for non-ambiguous entities

    # Check for context match
    for keyword in keywords:
        if keyword in context_window:
            return True

    return False

def enrich_articles(articles, entities):
    """
    Adds implicit fields to each article:
      - implicit_parties: parties inferred from mentioned ministers/CMs
      - implicit_states: states inferred from mentioned ministers/CMs
      - all_parties: union of explicit + implicit parties
      - all_states: union of explicit + implicit states
      - all_ministers: canonical minister names (alias resolution)

    Also scans article text for unmentioned but matching ministers/parties/states
    using simple substring matching.
    """
    logging.info("Enriching articles with implicit entity links...")

    all_ministers = (
        entities['india']['cabinet_ministers'] +
        entities['india']['state_chief_ministers'] +
        entities['india']['opposition_leaders']
    )

    # Build lookup maps
    minister_to_party = {}
    minister_to_state = {}
    minister_aliases = {}  # alias -> canonical
    for m in all_ministers:
        canonical = m['name']
        minister_to_party[canonical] = m.get('party', '')
        minister_to_state[canonical] = m.get('state', '')
        minister_aliases[canonical.lower()] = canonical
        for alias in m.get('aliases', []):
            minister_aliases[alias.lower()] = canonical

    party_aliases = {}
    for p in entities['india']['parties']:
        canonical = p['name']
        party_aliases[canonical.lower()] = canonical
        for alias in p.get('aliases', []):
            party_aliases[alias.lower()] = canonical

    state_aliases = {}
    state_to_ruling_party = {}
    for s in entities['india']['states']:
        canonical = s['name']
        state_aliases[canonical.lower()] = canonical
        for alias in s.get('aliases', []):
            state_aliases[alias.lower()] = canonical
        state_to_ruling_party[canonical] = s.get('ruling_party', '')

    # Sources that are NOT Indian — skip implicit Indian entity linking for these
    NON_INDIAN_SOURCES = {'The Dawn', 'BBC', 'Al Jazeera', 'The Guardian'}

    enriched_count = 0
    debug_samples = []
    for article in articles:
        text = f"{article.get('title', '')} {article.get('content', '')[:1500]}"
        text_lower = text.lower()

        explicit_parties = set(article.get('party_mentioned', []))
        explicit_ministers = set(article.get('ministers_mentioned', []))
        explicit_states = set(article.get('states_mentioned', []))

        # --- HARD FILTER: Skip enrichment for non-Indian sources ---
        # These foreign sources mention Indian entities only when truly relevant
        # so we trust the explicit classifier output here
        if article.get('source') in NON_INDIAN_SOURCES:
            article['all_parties'] = list(explicit_parties)
            article['all_states'] = list(explicit_states)
            article['all_ministers'] = list(explicit_ministers)
            article['implicit_parties'] = []
            article['implicit_states'] = []
            continue

        implicit_parties = set()
        implicit_states = set()
        all_ministers_canonical = set()

        # --- Scan text for any minister mentions with context checks ---
        for alias_lower, canonical in minister_aliases.items():
            # Length threshold decreased to >= 3 to catch common 4-letter surnames
            if len(alias_lower) >= 3:
                pattern = r'\b' + re.escape(alias_lower) + r'\b'
                for match in re.finditer(pattern, text_lower):
                    # Ambiguity check for common surnames
                    if alias_lower in {"modi", "shah", "yadav", "gandhi", "banerjee", "kejriwal"}:
                        if verify_surname_context(match, text_lower, canonical):
                            all_ministers_canonical.add(canonical)
                    else:
                        all_ministers_canonical.add(canonical)

        # Add already-mentioned ministers
        for m_name in explicit_ministers:
            canonical = minister_aliases.get(m_name.lower(), m_name)
            all_ministers_canonical.add(canonical)

        # --- Infer parties/states from ministers ---
        for canonical in all_ministers_canonical:
            party = minister_to_party.get(canonical)
            state = minister_to_state.get(canonical)
            if party:
                implicit_parties.add(party)
            if state:
                implicit_states.add(state)

        # --- Scan text for state aliases (catches missed ones) ---
        for alias_lower, canonical in state_aliases.items():
            if len(alias_lower) >= 4:
                pattern = r'\b' + re.escape(alias_lower) + r'\b'
                if re.search(pattern, text_lower):
                    implicit_states.add(canonical)

        # NOTE: We do NOT auto-add a state's ruling party just because the state is mentioned.
        # That caused too many false positives (e.g. SEBI article mentioning Jharkhand → tagged JMM)
        # Parties are only added if they appear explicitly OR via minister inference.

        # --- Scan text for party aliases (catches missed ones) ---
        for alias_lower, canonical in party_aliases.items():
            if len(alias_lower) >= 3:
                pattern = r'\b' + re.escape(alias_lower) + r'\b'
                if re.search(pattern, text_lower):
                    # Extra check for "Congress" — only count if Indian context
                    if canonical in ['INC', 'Congress']:
                        if 'us congress' in text_lower or 'american congress' in text_lower or 'congressional' in text_lower:
                            continue
                    implicit_parties.add(canonical)

        # --- For international articles, still do implicit linking but more conservatively ---
        # If the article explicitly mentions Indian ministers/parties, still link them
        # But don't add state ruling parties for international articles
        if article.get('category') == 'international':
            # Only keep ministers/parties found explicitly in text — not derived from states
            article['all_parties'] = list(explicit_parties | implicit_parties)
            article['all_states'] = list(explicit_states | implicit_states)
            article['all_ministers'] = list(all_ministers_canonical)
            article['implicit_parties'] = list(implicit_parties - explicit_parties)
            article['implicit_states'] = list(implicit_states - explicit_states)
            if implicit_parties or implicit_states or all_ministers_canonical:
                enriched_count += 1
            continue

        # --- Merge all ---
        article['implicit_parties'] = list(implicit_parties - explicit_parties)
        article['implicit_states'] = list(implicit_states - explicit_states)
        article['all_parties'] = list(explicit_parties | implicit_parties)
        article['all_states'] = list(explicit_states | implicit_states)
        article['all_ministers'] = list(all_ministers_canonical)

        if implicit_parties or implicit_states:
            enriched_count += 1
            if len(debug_samples) < 5:
                debug_samples.append({
                    "title": article.get('title', '')[:80],
                    "explicit_parties": list(explicit_parties),
                    "implicit_parties": list(implicit_parties),
                    "all_parties": article.get('all_parties', []),
                    "all_states": article.get('all_states', []),
                    "all_ministers": article.get('all_ministers', [])
                })

    logging.info(f"Enriched {enriched_count}/{len(articles)} articles with implicit entity links.")
    for sample in debug_samples:
        logging.info(f"  DEBUG: {sample}")
    return articles

# ==============================================================================
# --- HELPERS ---
# ==============================================================================

def slugify(name):
    """Convert 'Uttar Pradesh' to 'uttar_pradesh'."""
    return re.sub(r'[^a-z0-9_]', '', name.lower().replace(' ', '_').replace('.', ''))

def serialize_article(article, include_full=False):
    """Convert article to a clean JSON-safe dict (without internal fields)."""
    clean = {
        "id": article.get('id'),
        "title": article.get('title', ''),
        "url": article.get('url', ''),
        "source": article.get('source', ''),
        "image_url": article.get('image_url', ''),
        "scraped_at": article.get('scraped_at', ''),
        "category": article.get('category', ''),
        "sentiment": article.get('sentiment', ''),
        "sentiment_target": article.get('sentiment_target', ''),
        "rephrased_article": article.get('rephrased_article', ''),
        "party_mentioned": article.get('party_mentioned', []),
        "ministers_mentioned": article.get('ministers_mentioned', []),
        "states_mentioned": article.get('states_mentioned', []),
        "cities_mentioned": article.get('cities_mentioned', []),
        "topic_tags": article.get('topic_tags', [])
    }
    if include_full:
        clean['content'] = article.get('content', '')
    return clean

def filter_recent(articles, days):
    cutoff = datetime.now() - timedelta(days=days)
    return [a for a in articles if a['scraped_dt'] >= cutoff]

def sort_by_date(articles, descending=True):
    return sorted(articles, key=lambda a: a['scraped_dt'], reverse=descending)

def save_json(data, filename):
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

# ==============================================================================
# --- BUILDERS ---
# ==============================================================================

def build_india_overview(articles, entities, promises):
    """The home page JSON — top stories + state of nation summary."""
    logging.info("Building india_overview.json")

    recent = filter_recent(articles, TOP_STORIES_DAYS)
    sorted_recent = sort_by_date(recent)

    # Top stories — negative + politics/economy/crime get priority
    top_stories = []
    for a in sorted_recent[:30]:
        priority_categories = {'politics', 'economy', 'crime', 'international'}
        if a.get('category') in priority_categories:
            top_stories.append(serialize_article(a))
        if len(top_stories) >= 10:
            break

    # If not enough, fill from recent
    if len(top_stories) < 10:
        for a in sorted_recent:
            sa = serialize_article(a)
            if sa not in top_stories:
                top_stories.append(sa)
            if len(top_stories) >= 10:
                break

    # Category breakdown
    category_counts = Counter(a.get('category', 'other') for a in recent)

    # Top mentioned ministers in last 30 days
    minister_counts = Counter()
    for a in filter_recent(articles, 30):
        for m in a.get('all_ministers', a.get('ministers_mentioned', [])):
            minister_counts[m] += 1

    # Top mentioned parties
    party_counts = Counter()
    for a in filter_recent(articles, 30):
        for p in a.get('all_parties', a.get('party_mentioned', [])):
            party_counts[p] += 1

    # Top mentioned states
    state_counts = Counter()
    for a in filter_recent(articles, 30):
        for s in a.get('all_states', a.get('states_mentioned', [])):
            state_counts[s] += 1

    # Promise stats
    promise_summary = {
        "total": 0,
        "kept": 0,
        "broken": 0,
        "ongoing": 0
    }
    if promises:
        for p in promises.get('promises', []):
            promise_summary['total'] += 1
            promise_summary[p.get('status', 'ongoing')] = promise_summary.get(p.get('status', 'ongoing'), 0) + 1

    # Civic flag stats
    flagged_count = sum(1 for a in filter_recent(articles, 30) if a.get('civic_flag'))
    flagged_today = sum(1 for a in filter_recent(articles, 1) if a.get('civic_flag'))

    # Top flag categories
    flag_categories = Counter(
        a.get('civic_flag_category', 'other')
        for a in filter_recent(articles, 30)
        if a.get('civic_flag')
    )

    overview = {
        "generated_at": str(datetime.now()),
        "current_government": {
            "ruling_party": entities['india']['central_government']['ruling_party'],
            "ruling_coalition": entities['india']['central_government']['ruling_coalition'],
            "prime_minister": entities['india']['central_government']['prime_minister'],
            "president": entities['india']['central_government']['president']
        },
        "stats": {
            "total_articles_classified": len(articles),
            "articles_last_7_days": len(filter_recent(articles, 7)),
            "articles_last_30_days": len(filter_recent(articles, 30)),
            "civic_flags_last_30_days": flagged_count,
            "civic_flags_today": flagged_today
        },
        "civic_alert": {
            "flagged_count_30d": flagged_count,
            "flagged_today": flagged_today,
            "top_flag_categories": dict(flag_categories.most_common(5))
        },
        "top_stories": top_stories,
        "category_breakdown_30d": dict(category_counts),
        # Removed .most_common(10) so we publish counts for all tracked netas/states in sorted order
        "top_ministers_30d": dict(minister_counts.most_common()),
        "top_parties_30d": dict(party_counts.most_common()),
        "top_states_30d": dict(state_counts.most_common()),
        "promise_summary": promise_summary
    }

    save_json(overview, 'india_overview.json')

def build_party_dashboards(articles, entities, promises):
    """One JSON per major party."""
    logging.info("Building party dashboards...")
    generated = []

    party_articles_map = defaultdict(list)
    for a in articles:
        for p in a.get('all_parties', a.get('party_mentioned', [])):
            party_articles_map[p].append(a)

    for party in entities['india']['parties']:
        party_name = party['name']
        party_articles = party_articles_map.get(party_name, [])

        if len(party_articles) < MIN_PARTY_ARTICLES:
            logging.info(f"  Skipping {party_name} (only {len(party_articles)} articles)")
            continue

        # Get recent articles
        recent = filter_recent(party_articles, RECENT_DAYS)
        sorted_articles = sort_by_date(party_articles)

        # Ministers belonging to this party
        party_ministers = []
        for m in entities['india']['cabinet_ministers'] + entities['india']['state_chief_ministers'] + entities['india']['opposition_leaders']:
            if m.get('party') == party_name:
                party_ministers.append({
                    "name": m['name'],
                    "role": m.get('role', ''),
                    "state": m.get('state', ''),
                    "criminal_cases": m.get('criminal_cases', 0),
                    "criminal_cases_in_news": m.get('criminal_cases_in_news', 0)
                })

        # Promises by this party
        party_promises = []
        if promises:
            for p in promises.get('promises', []):
                if p.get('party') == party_name:
                    party_promises.append({
                        "id": p['id'],
                        "person": p['person'],
                        "promise": p['promise'],
                        "status": p['status'],
                        "category": p.get('category', ''),
                        "evidence_count": len(p.get('evidence_articles', [])),
                        "evidence_articles": p.get('evidence_articles', [])
                    })

        # Sentiment breakdown
        sentiment_counts = Counter(a.get('sentiment', 'neutral') for a in recent)

        dashboard = {
            "generated_at": str(datetime.now()),
            "party": party_name,
            "full_name": party.get('full_name', ''),
            "ideology": party.get('ideology', ''),
            "president": party.get('president', ''),
            "coalition": party.get('coalition', ''),
            "ruling_states": party.get('ruling_states', []),
            "color": party.get('color', ''),
            "stats": {
                "total_articles": len(party_articles),
                "articles_last_30d": len(recent),
                "sentiment_breakdown_30d": dict(sentiment_counts)
            },
            "ministers": party_ministers,
            "promises": party_promises,
            "recent_articles": [serialize_article(a) for a in sorted_articles[:MAX_ARTICLES_PER_JSON]]
        }

        save_json(dashboard, f'party_{slugify(party_name)}.json')
        generated.append(party_name)
        logging.info(f"  Generated party_{slugify(party_name)}.json ({len(party_articles)} articles)")

    return generated

def build_state_pages(articles, entities, promises):
    """One JSON per state with sufficient article coverage."""
    logging.info("Building state pages...")
    generated = []

    state_articles_map = defaultdict(list)
    for a in articles:
        for s in a.get('all_states', a.get('states_mentioned', [])):
            state_articles_map[s].append(a)

    for state in entities['india']['states']:
        state_name = state['name']
        state_articles = state_articles_map.get(state_name, [])

        if len(state_articles) < MIN_STATE_ARTICLES:
            continue

        recent = filter_recent(state_articles, RECENT_DAYS)
        sorted_articles = sort_by_date(state_articles)

        # Cities in this state with articles
        city_counts = Counter()
        for a in recent:
            for c in a.get('cities_mentioned', []):
                city_counts[c] += 1

        # Top topics
        topic_counts = Counter()
        for a in recent:
            for t in a.get('topic_tags', []):
                topic_counts[t] += 1

        dashboard = {
            "generated_at": str(datetime.now()),
            "state": state_name,
            "capital": state.get('capital', ''),
            "ruling_party": state.get('ruling_party', ''),
            "cm": state.get('cm', ''),
            "region": state.get('region', ''),
            "cm_confidence": state.get('cm_confidence'),
            "party_confidence": state.get('party_confidence'),
            "stats": {
                "total_articles": len(state_articles),
                "articles_last_30d": len(recent)
            },
            "top_cities_30d": dict(city_counts.most_common(10)),
            "top_topics_30d": dict(topic_counts.most_common(10)),
            "recent_articles": [serialize_article(a) for a in sorted_articles[:MAX_ARTICLES_PER_JSON]]
        }

        save_json(dashboard, f'state_{slugify(state_name)}.json')
        generated.append(state_name)
        logging.info(f"  Generated state_{slugify(state_name)}.json ({len(state_articles)} articles)")

    return generated

def build_minister_pages(articles, entities, promises):
    """One JSON per minister with sufficient mentions."""
    logging.info("Building minister pages...")
    generated = []

    all_ministers = (
        entities['india']['cabinet_ministers'] +
        entities['india']['state_chief_ministers'] +
        entities['india']['opposition_leaders']
    )

    minister_articles_map = defaultdict(list)
    minister_lookup = {}
    for m in all_ministers:
        names = [m['name']] + m.get('aliases', [])
        for n in names:
            minister_lookup[n.lower()] = m['name']

    for a in articles:
        mentioned = set()
        for name in a.get('all_ministers', a.get('ministers_mentioned', [])):
            canonical = minister_lookup.get(name.lower(), name)
            mentioned.add(canonical)
        for canonical in mentioned:
            minister_articles_map[canonical].append(a)

    for minister in all_ministers:
        m_name = minister['name']
        m_articles = minister_articles_map.get(m_name, [])

        if len(m_articles) < MIN_MINISTER_ARTICLES:
            continue

        recent = filter_recent(m_articles, RECENT_DAYS)
        sorted_articles = sort_by_date(m_articles)

        # Promises by this person
        person_promises = []
        if promises:
            for p in promises.get('promises', []):
                if p.get('person') == m_name:
                    person_promises.append({
                        "id": p['id'],
                        "promise": p['promise'],
                        "status": p['status'],
                        "made_on": p.get('made_on', ''),
                        "evidence_count": len(p.get('evidence_articles', [])),
                        "evidence_articles": p.get('evidence_articles', [])
                    })

        # Sentiment
        sentiment_counts = Counter(a.get('sentiment', 'neutral') for a in recent)

        profile = {
            "generated_at": str(datetime.now()),
            "name": m_name,
            "role": minister.get('role', ''),
            "ministry": minister.get('ministry', ''),
            "party": minister.get('party', ''),
            "state": minister.get('state', ''),
            "constituency": minister.get('constituency', ''),
            "criminal_cases": minister.get('criminal_cases', 0),
            "criminal_cases_in_news": minister.get('criminal_cases_in_news', 0),
            "criminal_incidents": minister.get('criminal_incidents', []),
            "wikipedia": minister.get('wikipedia', ''),
            "affidavit_url": minister.get('affidavit_url', ''),
            "stats": {
                "total_articles": len(m_articles),
                "articles_last_30d": len(recent),
                "sentiment_breakdown_30d": dict(sentiment_counts)
            },
            "promises": person_promises,
            "recent_articles": [serialize_article(a) for a in sorted_articles[:MAX_ARTICLES_PER_JSON]]
        }

        save_json(profile, f'minister_{slugify(m_name)}.json')
        generated.append(m_name)
        logging.info(f"  Generated minister_{slugify(m_name)}.json ({len(m_articles)} articles)")

    return generated

def build_topic_pages(articles, entities):
    """One JSON per topic with sufficient article coverage."""
    logging.info("Building topic pages...")
    generated = []

    topic_articles_map = defaultdict(list)
    for a in articles:
        for t in a.get('topic_tags', []):
            topic_articles_map[t].append(a)

    # Also generate by category
    category_articles_map = defaultdict(list)
    for a in articles:
        cat = a.get('category', 'other')
        category_articles_map[cat].append(a)

    # Build topic pages
    for topic, t_articles in topic_articles_map.items():
        if len(t_articles) < MIN_TOPIC_ARTICLES:
            continue
        sorted_articles = sort_by_date(t_articles)
        page = {
            "generated_at": str(datetime.now()),
            "topic": topic,
            "stats": {
                "total_articles": len(t_articles),
                "articles_last_30d": len(filter_recent(t_articles, 30))
            },
            "recent_articles": [serialize_article(a) for a in sorted_articles[:MAX_ARTICLES_PER_JSON]]
        }
        save_json(page, f'topic_{slugify(topic)}.json')
        generated.append(topic)
        logging.info(f"  Generated topic_{slugify(topic)}.json ({len(t_articles)} articles)")

    # Build category pages
    for category, c_articles in category_articles_map.items():
        if len(c_articles) < MIN_TOPIC_ARTICLES:
            continue
        sorted_articles = sort_by_date(c_articles)
        page = {
            "generated_at": str(datetime.now()),
            "category": category,
            "stats": {
                "total_articles": len(c_articles),
                "articles_last_30d": len(filter_recent(c_articles, 30))
            },
            "recent_articles": [serialize_article(a) for a in sorted_articles[:MAX_ARTICLES_PER_JSON]]
        }
        save_json(page, f'category_{slugify(category)}.json')
        generated.append(f"category_{category}")
        logging.info(f"  Generated category_{slugify(category)}.json ({len(c_articles)} articles)")

    return generated

def build_promises_summary(promises):
    """Promise tracker summary JSON."""
    if not promises:
        return False
    logging.info("Building promises_summary.json")

    by_status = defaultdict(list)
    by_person = defaultdict(list)
    by_party = defaultdict(list)

    for p in promises.get('promises', []):
        status = p.get('status', 'ongoing')
        person = p.get('person', '')
        party = p.get('party', '')

        light = {
            "id": p['id'],
            "person": person,
            "party": party,
            "promise": p['promise'],
            "category": p.get('category', ''),
            "status": status,
            "made_on": p.get('made_on', ''),
            "deadline": p.get('deadline', ''),
            "evidence_count": len(p.get('evidence_articles', [])),
            "evidence_articles": p.get('evidence_articles', []),  # <-- Injected: Sends the actual articles to the frontend!
            "gemma_suggestion": p.get('gemma_suggestion'),
            "gemma_reasoning": p.get('gemma_reasoning')
        }

        by_status[status].append(light)
        by_person[person].append(light)
        if party:
            by_party[party].append(light)

    summary = {
        "generated_at": str(datetime.now()),
        "stats": {
            "total_promises": len(promises.get('promises', [])),
            "kept": len(by_status.get('kept', [])),
            "broken": len(by_status.get('broken', [])),
            "ongoing": len(by_status.get('ongoing', []))
        },
        "by_status": dict(by_status),
        "by_person": dict(by_person),
        "by_party": dict(by_party)
    }

    save_json(summary, 'promises_summary.json')
    return True

def build_manifest(parties, states, ministers, topics, has_promises):
    """Index of all available JSON files."""
    logging.info("Building manifest.json")
    manifest = {
        "generated_at": str(datetime.now()),
        "endpoints": {
            "feed": "feed.json",
            "feed_flagged": "feed_flagged.json",
            "feed_politics": "feed_politics.json",
            "feed_crime": "feed_crime.json",
            "feed_economy": "feed_economy.json",
            "feed_international": "feed_international.json",
            "feed_health": "feed_health.json",
            "feed_corruption": "feed_topic_corruption.json",
            "feed_farmers": "feed_topic_farmers.json",
            "india_overview": "india_overview.json",
            "promises_summary": "promises_summary.json" if has_promises else None,
            "parties": {p: f"party_{slugify(p)}.json" for p in parties},
            "states": {s: f"state_{slugify(s)}.json" for s in states},
            "ministers": {m: f"minister_{slugify(m)}.json" for m in ministers},
            "topics": {t: (f"category_{slugify(t.replace('category_',''))}.json" if t.startswith('category_') else f"topic_{slugify(t)}.json") for t in topics}
        },
        "stats": {
            "parties_count": len(parties),
            "states_count": len(states),
            "ministers_count": len(ministers),
            "topics_count": len(topics)
        }
    }
    save_json(manifest, 'manifest.json')

def is_india_centered(article):
    """
    Returns True if an article is India-centered.
    Checks source, states, ministers, parties, category.
    """
    INDIAN_SOURCES = {'The Hindu', 'Times of India', 'Economic Times'}
    INDIA_CATEGORIES = {'politics', 'crime', 'regional'}

    if article.get('source') in INDIAN_SOURCES:
        return True
    if article.get('states_mentioned') or article.get('all_states'):
        return True
    if article.get('ministers_mentioned') or article.get('all_ministers'):
        return True
    if article.get('all_parties') or article.get('party_mentioned'):
        return True
    if article.get('category') in INDIA_CATEGORIES:
        return True
    return False

def clean_markdown(text):
    """Strip **bold** markdown from text for clean display."""
    if not text:
        return text
    return re.sub(r'\*\*(.*?)\*\*', r'\1', text)

def build_feeds(articles):
    """
    Builds the main homepage feed and category feeds.
    Main feed: 10000 articles, 70%+ India-centered.
    Category feeds: 2000 articles each.
    """
    logging.info("Building feed JSONs...")

    sorted_articles = sort_by_date(articles)

    # --- Split into India-centered and international ---
    india_articles = [a for a in sorted_articles if is_india_centered(a)]
    international_articles = [a for a in sorted_articles if not is_india_centered(a)]

    logging.info(f"  India-centered: {len(india_articles)} | International: {len(international_articles)}")

    # --- Build main feed: 9000 India + 1000 International (or whatever is available) ---
    india_quota = min(9000, len(india_articles))
    international_quota = min(1000, len(international_articles))

    # If India articles are fewer than 9000, fill up with international
    if india_quota < 9000:
        international_quota = min(10000 - india_quota, len(international_articles))

    feed_articles = india_articles[:india_quota] + international_articles[:international_quota]

    # Re-sort combined feed by date
    feed_articles = sort_by_date(feed_articles)[:10000]

    india_count = sum(1 for a in feed_articles if is_india_centered(a))
    india_pct = round(india_count / len(feed_articles) * 100) if feed_articles else 0

    logging.info(f"  Main feed: {len(feed_articles)} articles ({india_pct}% India-centered)")

    # Clean markdown from rephrased articles
    def clean_article(a):
        cleaned = serialize_article(a)
        cleaned['rephrased_article'] = clean_markdown(cleaned.get('rephrased_article', ''))
        cleaned['is_india'] = is_india_centered(a)
        cleaned['civic_flag'] = a.get('civic_flag', False)
        cleaned['civic_flag_score'] = a.get('civic_flag_score', 0)
        cleaned['civic_flag_category'] = a.get('civic_flag_category')
        cleaned['civic_flag_reason'] = a.get('civic_flag_reason')
        return cleaned

    feed = {
        "generated_at": str(datetime.now()),
        "total": len(feed_articles),
        "india_centered_count": india_count,
        "india_centered_pct": india_pct,
        "articles": [clean_article(a) for a in feed_articles]
    }
    save_json(feed, 'feed.json')
    logging.info(f"  Saved feed.json ({len(feed_articles)} articles)")

    # --- Category feeds: 200 each ---
    category_map = {
        'politics': 'feed_politics.json',
        'crime': 'feed_crime.json',
        'economy': 'feed_economy.json',
        'international': 'feed_international.json',
        'health': 'feed_health.json',
        'education': 'feed_education.json',
        'other': 'feed_other.json',
    }

    for category, filename in category_map.items():
        cat_articles = [a for a in sorted_articles if a.get('category') == category]
        cat_articles = cat_articles[:200]
        if cat_articles:
            cat_feed = {
                "generated_at": str(datetime.now()),
                "category": category,
                "total": len(cat_articles),
                "articles": [clean_article(a) for a in cat_articles]
            }
            save_json(cat_feed, filename)
            logging.info(f"  Saved {filename} ({len(cat_articles)} articles)")

    # --- Topic feeds for homepage filter ---
    topic_map = {
        'corruption_scam': 'feed_topic_corruption.json',
        'rape_sexual_crime': 'feed_topic_crime_against_women.json',
        'farmer_agriculture': 'feed_topic_farmers.json',
        'foreign_policy': 'feed_topic_foreign.json',
    }

    for topic, filename in topic_map.items():
        topic_articles = [a for a in sorted_articles if topic in a.get('topic_tags', [])]
        topic_articles = topic_articles[:200]
        if topic_articles:
            topic_feed = {
                "generated_at": str(datetime.now()),
                "topic": topic,
                "total": len(topic_articles),
                "articles": [clean_article(a) for a in topic_articles]
            }
            save_json(topic_feed, filename)
            logging.info(f"  Saved {filename} ({len(topic_articles)} articles)")

    # --- Build flagged feed: "What's Wrong Right Now" ---
    flagged_articles = [
        a for a in sorted_articles
        if a.get('civic_flag') is True
    ]

    # Sort by flag score descending, then by date
    flagged_articles.sort(
        key=lambda a: (a.get('civic_flag_score', 0), a.get('scraped_at', '')),
        reverse=True
    )

    if flagged_articles:
        # Group by flag category
        by_category = defaultdict(list)
        for a in flagged_articles:
            cat = a.get('civic_flag_category', 'other')
            by_category[cat].append(a)

        flagged_feed = {
            "generated_at": str(datetime.now()),
            "total": len(flagged_articles),
            "description": "Articles flagged as needing immediate public attention",
            "category_breakdown": {k: len(v) for k, v in by_category.items()},
            "articles": [clean_article(a) for a in flagged_articles[:500]]
        }
        save_json(flagged_feed, 'feed_flagged.json')
        logging.info(f"  Saved feed_flagged.json ({len(flagged_articles)} flagged articles)")
    else:
        logging.info("  No civic-flagged articles found yet (classifier needs to run with new version)")

    return len(feed_articles)

# ==============================================================================
# --- MAIN ---
# ==============================================================================

def main():
    start_time = time.time()
    logging.info("--- Satya Aggregator Started ---")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1. Load all data
    sheet = connect_to_sheets()
    articles = fetch_articles(sheet)

    entities = load_json_from_url(ENTITIES_JSON_URL, './entities.json')
    promises = load_json_from_url(PROMISES_JSON_URL, './promises.json')

    if not entities:
        logging.critical("Could not load entities.json. Exiting.")
        return

    if not promises:
        logging.warning("Could not load promises.json. Promises features will be limited.")

    # 1.5. Enrich articles with implicit entity links
    articles = enrich_articles(articles, entities)

    # 2. Build all JSON outputs
    feed_count = build_feeds(articles)
    build_india_overview(articles, entities, promises)
    parties = build_party_dashboards(articles, entities, promises)
    states = build_state_pages(articles, entities, promises)
    ministers = build_minister_pages(articles, entities, promises)
    topics = build_topic_pages(articles, entities)
    has_promises = build_promises_summary(promises)

    # 3. Build manifest
    build_manifest(parties, states, ministers, topics, has_promises)

    elapsed = round(time.time() - start_time, 2)
    logging.info(f"--- Aggregator Finished in {elapsed}s ---")
    logging.info(f"Generated: {len(parties)} parties, {len(states)} states, {len(ministers)} ministers, {len(topics)} topics, feed: {feed_count} articles")

    print(json.dumps({
        "feed_articles": feed_count,
        "parties": len(parties),
        "states": len(states),
        "ministers": len(ministers),
        "topics": len(topics),
        "promises_summary": has_promises
    }, indent=2))


if __name__ == '__main__':
    main()
