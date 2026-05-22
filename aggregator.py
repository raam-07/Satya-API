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
MIN_STATE_ARTICLES = 30        # Min articles to generate state JSON
MIN_MINISTER_ARTICLES = 20     # Min articles to generate minister JSON
MIN_PARTY_ARTICLES = 25        # Min articles to generate party JSON
MIN_TOPIC_ARTICLES = 30        # Min articles to generate topic JSON
MIN_CITY_ARTICLES = 25         # Min articles to generate city JSON

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
        for m in a.get('ministers_mentioned', []):
            minister_counts[m] += 1

    # Top mentioned parties
    party_counts = Counter()
    for a in filter_recent(articles, 30):
        for p in a.get('party_mentioned', []):
            party_counts[p] += 1

    # Top mentioned states
    state_counts = Counter()
    for a in filter_recent(articles, 30):
        for s in a.get('states_mentioned', []):
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
            "articles_last_30_days": len(filter_recent(articles, 30))
        },
        "top_stories": top_stories,
        "category_breakdown_30d": dict(category_counts),
        "top_ministers_30d": dict(minister_counts.most_common(10)),
        "top_parties_30d": dict(party_counts.most_common(10)),
        "top_states_30d": dict(state_counts.most_common(10)),
        "promise_summary": promise_summary
    }

    save_json(overview, 'india_overview.json')

def build_party_dashboards(articles, entities, promises):
    """One JSON per major party."""
    logging.info("Building party dashboards...")
    generated = []

    party_articles_map = defaultdict(list)
    for a in articles:
        for p in a.get('party_mentioned', []):
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
                        "category": p.get('category', '')
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
        for s in a.get('states_mentioned', []):
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
        for name in a.get('ministers_mentioned', []):
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
                        "evidence_count": len(p.get('evidence_articles', []))
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

    # 2. Build all JSON outputs
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
    logging.info(f"Generated: {len(parties)} parties, {len(states)} states, {len(ministers)} ministers, {len(topics)} topics")

    print(json.dumps({
        "parties": len(parties),
        "states": len(states),
        "ministers": len(ministers),
        "topics": len(topics),
        "promises_summary": has_promises
    }, indent=2))


if __name__ == '__main__':
    main()
