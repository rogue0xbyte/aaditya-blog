from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import markdown
import re
from functools import wraps
import requests
from datetime import datetime
import xml.etree.ElementTree as ET
import html
import re

from dotenv import load_dotenv
import os

load_dotenv()

from mangum import Mangum

app = Flask(__name__, template_folder="../templates", static_folder="../static")
app.secret_key = secrets.token_hex(32)

# MongoDB connection
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client['blog_database']
categories_collection = db['categories']
posts_collection = db['posts']
otps_collection = db['otps']
books_collection = db['books']
books_sync_collection = db['books_sync']
films_collection = db['films']
films_sync_collection = db['films_sync']

# Admin password
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL')

SMTP_SERVER = os.getenv('SMTP_SERVER')
SMTP_PORT = os.getenv('SMTP_PORT')
SMTP_EMAIL = os.getenv('SMTP_EMAIL')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')

LITERAL_API_URL = "https://literal.club/graphql/"
LITERAL_HANDLE = "epiphany"

# GraphQL Queries
PROFILE_QUERY = """
query profile($handle: String!) {
    profile(where: { handle: $handle }) {
        id
        handle
        name
        bio
        image
    }
}
"""

BOOKS_QUERY = """
query booksByReadingStateAndProfile($limit: Int!, $offset: Int!, $readingStatus: ReadingStatus!, $profileId: String!) {
    booksByReadingStateAndProfile(
        limit: $limit
        offset: $offset
        readingStatus: $readingStatus
        profileId: $profileId
    ) {
        id
        slug
        title
        subtitle
        description
        cover
        authors {
            id
            name
        }
    }
}
"""

READING_STATES_QUERY = """
query readingStatesByProfile($limit: Int!, $offset: Int!, $profileId: String!) {
    readingStatesByProfile(
        limit: $limit
        offset: $offset
        profileId: $profileId
    ) {
        id
        status
        book {
            id
            slug
            title
            subtitle
            description
            cover
            authors {
                id
                name
            }
        }
        rating
        createdAt
        completedAt
        review
    }
}
"""

LETTERBOXD_USERNAME = "prettyboiiii"
LETTERBOXD_RSS_URL = f"https://letterboxd.com/{LETTERBOXD_USERNAME}/rss/"

def sync_literal_books():
    """Fetch and sync Literal.club books to MongoDB."""
    try:
        # Fetch profile
        profile = fetch_profile(LITERAL_HANDLE)
        
        if not profile:
            print("Error: Could not fetch Literal.club profile")
            return False
        
        profile_id = profile['id']
        
        # Fetch all reading states to get reviews and ratings
        reading_states = fetch_reading_states(profile_id)
        
        # Create a mapping of book_id to reading state data
        book_metadata = {}
        for state in reading_states:
            if state.get('book'):
                book_id = state['book']['id']
                book_metadata[book_id] = {
                    'rating': state.get('rating'),
                    'review': state.get('review'),
                    'completed_date': format_date(state.get('completedAt')),
                    'status': state.get('status')
                }
        
        # Fetch books by status
        currently_reading = fetch_books_by_status(profile_id, "IS_READING")
        finished = fetch_books_by_status(profile_id, "FINISHED")
        want_to_read = fetch_books_by_status(profile_id, "WANTS_TO_READ")
        
        # Enrich all books with metadata and status
        for book in currently_reading:
            metadata = book_metadata.get(book['id'], {})
            book['rating'] = metadata.get('rating')
            book['review'] = metadata.get('review')
            book['completed_date'] = metadata.get('completed_date')
            book['reading_status'] = 'currently_reading'
            book['synced_at'] = datetime.utcnow()
        
        for book in finished:
            metadata = book_metadata.get(book['id'], {})
            book['rating'] = metadata.get('rating')
            book['review'] = metadata.get('review')
            book['completed_date'] = metadata.get('completed_date')
            book['reading_status'] = 'finished'
            book['synced_at'] = datetime.utcnow()
        
        for book in want_to_read:
            metadata = book_metadata.get(book['id'], {})
            book['rating'] = metadata.get('rating')
            book['review'] = metadata.get('review')
            book['completed_date'] = metadata.get('completed_date')
            book['reading_status'] = 'want_to_read'
            book['synced_at'] = datetime.utcnow()
        
        # Combine all books
        all_books = currently_reading + finished + want_to_read
        
        # Clear old data and insert new data
        if all_books:
            books_collection.delete_many({})
            books_collection.insert_many(all_books)
            
            # Update sync timestamp
            books_sync_collection.delete_many({})
            books_sync_collection.insert_one({
                'last_synced': datetime.utcnow(),
                'book_count': len(all_books),
                'currently_reading_count': len(currently_reading),
                'finished_count': len(finished),
                'want_to_read_count': len(want_to_read)
            })
            
            print(f"Synced {len(all_books)} books to database")
            return True
        
        return False
    
    except Exception as e:
        print(f"Error syncing Literal.club books: {e}")
        return False

def should_sync_books():
    """Check if we should sync books data (once per hour)."""
    sync_record = books_sync_collection.find_one()
    
    if not sync_record:
        return True
    
    last_synced = sync_record.get('last_synced')
    if not last_synced:
        return True
    
    # Sync if more than 5 minutes has passed
    time_diff = (datetime.utcnow() - last_synced).total_seconds()
    return time_diff > 300  # 5 minutes

def fetch_profile(handle):
    """Fetch profile information by handle."""
    try:
        response = requests.post(
            LITERAL_API_URL,
            json={"query": PROFILE_QUERY, "variables": {"handle": handle}},
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        return data.get("data", {}).get("profile")
    except Exception as e:
        print(f"Error fetching profile: {e}")
        return None

def fetch_books_by_status(profile_id, status):
    """Fetch books by reading status."""
    try:
        response = requests.post(
            LITERAL_API_URL,
            json={
                "query": BOOKS_QUERY,
                "variables": {
                    "limit": 50,
                    "offset": 0,
                    "readingStatus": status,
                    "profileId": profile_id
                }
            },
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        return data.get("data", {}).get("booksByReadingStateAndProfile", [])
    except Exception as e:
        print(f"Error fetching books for status {status}: {e}")
        return []

def fetch_reading_states(profile_id):
    """Fetch reading states with additional metadata like ratings and reviews."""
    try:
        response = requests.post(
            LITERAL_API_URL,
            json={
                "query": READING_STATES_QUERY,
                "variables": {
                    "limit": 100,
                    "offset": 0,
                    "profileId": profile_id
                }
            },
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        return data.get("data", {}).get("readingStatesByProfile", [])
    except Exception as e:
        print(f"Error fetching reading states: {e}")
        return []

def format_date(date_string):
    """Format ISO date string to readable format."""
    if not date_string:
        return None
    try:
        dt = datetime.fromisoformat(date_string.replace('Z', '+00:00'))
        return dt.strftime('%b %d, %Y')
    except:
        return None

def sync_letterboxd_rss():
    """Fetch and sync Letterboxd RSS feed to MongoDB."""
    try:
        response = requests.get(LETTERBOXD_RSS_URL, timeout=10)
        response.raise_for_status()
        
        # Parse XML with namespaces
        root = ET.fromstring(response.content)
        
        # Define namespaces
        namespaces = {
            'letterboxd': 'https://letterboxd.com',
            'tmdb': 'https://themoviedb.org',
            'dc': 'http://purl.org/dc/elements/1.1/'
        }
        
        items = []
        
        # Find all items in the feed
        for item in root.findall('.//item'):
            # Extract data using namespace-aware methods
            title = item.find('title').text if item.find('title') is not None else 'Unknown'
            link = item.find('link').text if item.find('link') is not None else ''
            pub_date = item.find('pubDate').text if item.find('pubDate') is not None else ''
            description = item.find('description').text if item.find('description') is not None else ''
            guid = item.find('guid').text if item.find('guid') is not None else ''
            
            # Extract Letterboxd-specific fields
            film_title_elem = item.find('letterboxd:filmTitle', namespaces)
            film_title = film_title_elem.text if film_title_elem is not None else None
            
            film_year_elem = item.find('letterboxd:filmYear', namespaces)
            film_year = film_year_elem.text if film_year_elem is not None else None
            
            rating_elem = item.find('letterboxd:memberRating', namespaces)
            rating = float(rating_elem.text) if rating_elem is not None and rating_elem.text else None
            
            watched_date_elem = item.find('letterboxd:watchedDate', namespaces)
            watched_date = watched_date_elem.text if watched_date_elem is not None else None
            
            rewatch_elem = item.find('letterboxd:rewatch', namespaces)
            is_rewatch = rewatch_elem.text == 'Yes' if rewatch_elem is not None else False
            
            # Extract TMDb ID
            tmdb_id_elem = item.find('tmdb:movieId', namespaces)
            tmdb_id = tmdb_id_elem.text if tmdb_id_elem is not None else None
            
            # Extract poster URL from description
            poster_url = None
            if description and '<img src=' in description:
                poster_match = re.search(r'<img src="([^"]+)"', description)
                if poster_match:
                    poster_url = poster_match.group(1)
            
            # Extract review text (remove HTML and image tags)
            review_text = None
            if description:
                # Remove CDATA markers if present
                clean_desc = description.replace('<![CDATA[', '').replace(']]>', '')
                # Remove image tags
                clean_desc = re.sub(r'<img[^>]+>', '', clean_desc)
                # Remove paragraph tags but keep content
                clean_desc = re.sub(r'<p>', '', clean_desc)
                clean_desc = re.sub(r'</p>', '\n', clean_desc)
                # Remove other HTML tags
                clean_desc = re.sub(r'<[^>]+>', '', clean_desc)
                # Unescape HTML entities
                clean_desc = html.unescape(clean_desc)
                # Clean up whitespace
                clean_desc = clean_desc.strip()
                # Only keep if it's more than just "Watched on..."
                if clean_desc and not clean_desc.startswith('Watched on'):
                    review_text = clean_desc
            
            # Parse date
            formatted_date = None
            if pub_date:
                try:
                    dt = datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %z')
                    formatted_date = dt.strftime('%b %d, %Y')
                except:
                    formatted_date = pub_date
            
            # Format watched date
            formatted_watched_date = None
            watched_date_sortkey = watched_date
            if watched_date:
                try:
                    dt = datetime.strptime(watched_date, '%Y-%m-%d')
                    formatted_watched_date = dt.strftime('%b %d, %Y')
                except:
                    formatted_watched_date = watched_date
            
            # Determine if it's a review or just a watch
            is_review = 'review' in guid
            
            # Generate star display
            stars_display = ''
            if rating:
                full_stars = int(rating)
                half_star = (rating - full_stars) >= 0.5
                stars_display = '★' * full_stars
                if half_star:
                    stars_display += '½'
                empty_stars = '☆' * (5 - full_stars - (1 if half_star else 0))
                stars_display += empty_stars
            
            items.append({
                'guid': guid,  # Unique identifier
                'film_title': film_title,
                'film_year': film_year,
                'rating': rating,
                'stars_display': stars_display,
                'link': link,
                'pub_date': formatted_date,
                'watched_date': formatted_watched_date,
                'watched_date_sortkey': watched_date_sortkey,
                'is_rewatch': is_rewatch,
                'review_text': review_text,
                'poster_url': poster_url,
                'tmdb_id': tmdb_id,
                'is_review': is_review,
                'synced_at': datetime.utcnow()
            })
        
        # Clear old data and insert new data
        if items:
            films_collection.delete_many({})
            films_collection.insert_many(items)
            
            # Update sync timestamp
            films_sync_collection.delete_many({})
            films_sync_collection.insert_one({
                'last_synced': datetime.utcnow(),
                'film_count': len(items)
            })
            
            print(f"Synced {len(items)} films to database")
            return True
        
        return False
    
    except Exception as e:
        print(f"Error syncing Letterboxd RSS feed: {e}")
        return False

def should_sync_letterboxd():
    """Check if we should sync Letterboxd data (once per hour)."""
    sync_record = films_sync_collection.find_one()
    
    if not sync_record:
        return True
    
    last_synced = sync_record.get('last_synced')
    if not last_synced:
        return True
    
    # Sync if more than 5 minutes has passed
    time_diff = (datetime.utcnow() - last_synced).total_seconds()
    return time_diff > 300  # 5 minutes

def send_otp_email(otp):
    """Send OTP to admin email"""
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_EMAIL
        msg['To'] = ADMIN_EMAIL
        msg['Subject'] = 'Blog Admin Login OTP'
        
        body = f'Your OTP for admin login is: {otp}\n\nThis OTP is valid for 10 minutes.'
        msg.attach(MIMEText(body, 'plain'))
        
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        text = msg.as_string()
        server.sendmail(SMTP_EMAIL, ADMIN_EMAIL, text)
        server.quit()
        return True
    except Exception as e:
        print(f"Error sending OTP: {e}")
        return False

def login_required(f):
    """Decorator to require admin login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_logged_in' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

def generate_abstract(content, custom_abstract=None):
    """Generate abstract from content or use custom"""
    if custom_abstract and custom_abstract.strip():
        return custom_abstract
    
    # Strip markdown and HTML
    plain_text = re.sub(r'[#*`\[\]()]+', '', content)
    plain_text = re.sub(r'<[^>]+>', '', plain_text)
    plain_text = ' '.join(plain_text.split())
    
    return plain_text[:250] + '...' if len(plain_text) > 250 else plain_text

@app.route('/')
def home():
    """Home page showing all visible posts"""
    visible_categories = list(categories_collection.find({'visible': True}).sort('name', 1))
    visible_posts = list(posts_collection.find({'visible': True}).sort('created_at', -1))
    
    # Add category name to each post
    for post in visible_posts:
        if post.get('category_id'):
            category = categories_collection.find_one({'_id': ObjectId(post['category_id'])})
            post['category_name'] = category['name'] if category else 'Uncategorized'
    
    return render_template('blog.html', 
                         categories=visible_categories, 
                         posts=visible_posts, 
                         current_page='home')

@app.route('/category/<category_id>')
def category_page(category_id):
    """Category page showing posts in that category"""
    category = categories_collection.find_one({'_id': ObjectId(category_id), 'visible': True})
    if not category:
        return "Category not found", 404
    
    visible_categories = list(categories_collection.find({'visible': True}).sort('name', 1))
    posts = list(posts_collection.find({
        'category_id': category_id, 
        'visible': True
    }).sort('created_at', -1))
    
    for post in posts:
        post['category_name'] = category['name']
    
    return render_template('blog.html', 
                         categories=visible_categories, 
                         posts=posts, 
                         current_page='category',
                         current_category=category)

@app.route('/post/<post_id>')
def view_post(post_id):
    """View individual post"""
    post = posts_collection.find_one({'_id': ObjectId(post_id), 'visible': True})
    if not post:
        return "Post not found", 404
    
    visible_categories = list(categories_collection.find({'visible': True}).sort('name', 1))
    
    # Get category name
    if post.get('category_id'):
        category = categories_collection.find_one({'_id': ObjectId(post['category_id'])})
        post['category_name'] = category['name'] if category else 'Uncategorized'
    
    # Convert markdown to HTML
    post['content_html'] = markdown.markdown(post['content'], extensions=['fenced_code', 'tables'])
    
    return render_template('post.html', 
                         categories=visible_categories, 
                         post=post)

# Admin routes
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Admin login page"""
    if request.method == 'POST':
        password = request.form.get('password')
        
        if password == ADMIN_PASSWORD:
            # Generate OTP
            otp = secrets.token_hex(3).upper()  # 6-character OTP
            
            # Store OTP in database
            otps_collection.delete_many({})  # Clear old OTPs
            otps_collection.insert_one({
                'otp': otp,
                'created_at': datetime.utcnow()
            })
            
            # Send OTP
            if send_otp_email(otp):
                session['otp_verified'] = False
                flash('OTP sent to your device!', 'success')
                return redirect(url_for('verify_otp'))
            else:
                flash('Error sending OTP. Check device configuration.', 'error')
        else:
            flash('Invalid password', 'error')
    
    return render_template('admin_login.html')

@app.route('/admin/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    """Verify OTP page"""
    if request.method == 'POST':
        entered_otp = request.form.get('otp', '').upper()
        
        # Check OTP from database
        stored_otp = otps_collection.find_one()
        
        if stored_otp and stored_otp['otp'] == entered_otp:
            # Check if OTP is still valid (10 minutes)
            time_diff = (datetime.utcnow() - stored_otp['created_at']).total_seconds()
            if time_diff < 600:  # 10 minutes
                session['admin_logged_in'] = True
                otps_collection.delete_many({})  # Clear OTP
                return redirect(url_for('admin_dashboard'))
            else:
                flash('OTP expired. Please login again.', 'error')
                return redirect(url_for('admin_login'))
        else:
            flash('Invalid OTP', 'error')
    
    return render_template('verify_otp.html')

@app.route('/admin/logout')
def admin_logout():
    """Logout admin"""
    session.clear()
    flash('Logged out successfully', 'success')
    return redirect(url_for('home'))

@app.route('/admin')
@login_required
def admin_dashboard():
    """Admin dashboard"""
    categories = list(categories_collection.find().sort('name', 1))
    posts = list(posts_collection.find().sort('created_at', -1))
    
    # Add category names to posts
    for post in posts:
        if post.get('category_id'):
            category = categories_collection.find_one({'_id': ObjectId(post['category_id'])})
            post['category_name'] = category['name'] if category else 'Uncategorized'
    
    return render_template('admin_dashboard.html', 
                         categories=categories, 
                         posts=posts)

# Category CRUD
@app.route('/admin/category/create', methods=['POST'])
@login_required
def create_category():
    """Create new category"""
    name = request.form.get('name')
    visible = request.form.get('visible') == 'on'
    
    if name:
        categories_collection.insert_one({
            'name': name,
            'visible': visible,
            'created_at': datetime.utcnow()
        })
        flash('Category created successfully', 'success')
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/category/<category_id>/edit', methods=['POST'])
@login_required
def edit_category(category_id):
    """Edit category"""
    name = request.form.get('name')
    visible = request.form.get('visible') == 'on'
    
    if name:
        categories_collection.update_one(
            {'_id': ObjectId(category_id)},
            {'$set': {
                'name': name,
                'visible': visible
            }}
        )
        flash('Category updated successfully', 'success')
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/category/<category_id>/delete', methods=['POST'])
@login_required
def delete_category(category_id):
    """Delete category"""
    categories_collection.delete_one({'_id': ObjectId(category_id)})
    # Also delete all posts in this category
    posts_collection.delete_many({'category_id': category_id})
    flash('Category deleted successfully', 'success')
    return redirect(url_for('admin_dashboard'))

# Post CRUD
@app.route('/admin/post/create', methods=['GET', 'POST'])
@login_required
def create_post():
    """Create new post"""
    if request.method == 'POST':
        title = request.form.get('title')
        tagline = request.form.get('tagline')
        abstract = request.form.get('abstract')
        content = request.form.get('content')
        category_id = request.form.get('category_id')
        visible = request.form.get('visible') == 'on'
        
        if title and content:
            generated_abstract = generate_abstract(content, abstract)
            
            posts_collection.insert_one({
                'title': title,
                'tagline': tagline,
                'abstract': generated_abstract,
                'content': content,
                'category_id': category_id,
                'visible': visible,
                'created_at': datetime.utcnow(),
                'updated_at': datetime.utcnow()
            })
            flash('Post created successfully', 'success')
            return redirect(url_for('admin_dashboard'))
    
    categories = list(categories_collection.find().sort('name', 1))
    return render_template('post_editor.html', 
                         categories=categories, 
                         post=None,
                         mode='create')

@app.route('/admin/post/<post_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_post(post_id):
    """Edit post"""
    post = posts_collection.find_one({'_id': ObjectId(post_id)})
    
    if request.method == 'POST':
        title = request.form.get('title')
        tagline = request.form.get('tagline')
        abstract = request.form.get('abstract')
        content = request.form.get('content')
        category_id = request.form.get('category_id')
        visible = request.form.get('visible') == 'on'
        
        if title and content:
            generated_abstract = generate_abstract(content, abstract)
            
            posts_collection.update_one(
                {'_id': ObjectId(post_id)},
                {'$set': {
                    'title': title,
                    'tagline': tagline,
                    'abstract': generated_abstract,
                    'content': content,
                    'category_id': category_id,
                    'visible': visible,
                    'updated_at': datetime.utcnow()
                }}
            )
            flash('Post updated successfully', 'success')
            return redirect(url_for('admin_dashboard'))
    
    categories = list(categories_collection.find().sort('name', 1))
    return render_template('post_editor.html', 
                         categories=categories, 
                         post=post,
                         mode='edit')

@app.route('/admin/post/<post_id>/delete', methods=['POST'])
@login_required
def delete_post(post_id):
    """Delete post"""
    posts_collection.delete_one({'_id': ObjectId(post_id)})
    flash('Post deleted successfully', 'success')
    return redirect(url_for('admin_dashboard'))

# API endpoint for markdown preview
@app.route('/api/preview', methods=['POST'])
@login_required
def preview_markdown():
    """Preview markdown as HTML"""
    content = request.json.get('content', '')
    html = markdown.markdown(content, extensions=['fenced_code', 'tables'])
    return jsonify({'html': html})

@app.route('/books')
def books():
    """Books page showing Literal.club reading lists"""
    visible_categories = list(categories_collection.find({'visible': True}).sort('name', 1))
    
    # Check if we should sync from Literal.club
    if should_sync_books():
        print("Syncing Literal.club data...")
        sync_literal_books()
    
    # Fetch books from database
    all_books = list(books_collection.find())
    
    # Separate by reading status
    currently_reading = [b for b in all_books if b.get('reading_status') == 'currently_reading']
    finished = [b for b in all_books if b.get('reading_status') == 'finished']
    want_to_read = [b for b in all_books if b.get('reading_status') == 'want_to_read']
    
    # Sort finished books by completion date (most recent first)
    finished.sort(key=lambda x: x.get('completed_date', ''), reverse=True)
    
    # Get sync info
    sync_record = books_sync_collection.find_one()
    last_synced = None
    if sync_record and sync_record.get('last_synced'):
        try:
            last_synced = sync_record['last_synced'].strftime('%b %d, %Y at %I:%M %p')
        except:
            pass
    
    return render_template('books.html',
                         error=len(all_books) == 0,
                         currently_reading=currently_reading,
                         finished=finished,
                         want_to_read=want_to_read,
                         categories=visible_categories,
                         last_synced=last_synced,
                         now=datetime.now(),
                         current_page='books')

@app.route('/films')
def films():
    """Films page showing Letterboxd activity"""
    visible_categories = list(categories_collection.find({'visible': True}).sort('name', 1))
    
    # Check if we should sync from Letterboxd
    if should_sync_letterboxd():
        print("Syncing Letterboxd data...")
        sync_letterboxd_rss()
    
    # Fetch films from database
    films = list(films_collection.find().sort('synced_at', -1))
    films.sort(key=lambda x: x.get('watched_date_sortkey', ''), reverse=True)
    import json
    films_without_id = [{k: v for k, v in film.items() if k != '_id'} for film in films]
    print(json.dumps(films_without_id, indent=4, default=str))

    # Separate reviews and watches
    reviews = [f for f in films if f.get('is_review')]
    watches = [f for f in films if not f.get('is_review')]
    
    # Get sync info
    sync_record = films_sync_collection.find_one()
    last_synced = None
    if sync_record and sync_record.get('last_synced'):
        try:
            last_synced = sync_record['last_synced'].strftime('%b %d, %Y at %I:%M %p')
        except:
            pass
    
    return render_template('films.html',
                         error=len(films) == 0,
                         films=films,
                         reviews=reviews,
                         watches=watches,
                         categories=visible_categories,
                         last_synced=last_synced,
                         now=datetime.now(),
                         current_page='films')

handler = Mangum(app)

if __name__ == '__main__':
    app.run(debug=True)