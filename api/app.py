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

# Admin password
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL')

SMTP_SERVER = os.getenv('SMTP_SERVER')
SMTP_PORT = os.getenv('SMTP_PORT')
SMTP_EMAIL = os.getenv('SMTP_EMAIL')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')

def send_otp_email(otp):
    """Send OTP to admin email"""
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_EMAIL
        msg['To'] = ADMIN_EMAIL
        msg['Subject'] = 'Admin Login OTP'
        
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
        print(f"Error sending email: {e}")
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
                flash('OTP sent to your email!', 'success')
                return redirect(url_for('verify_otp'))
            else:
                flash('Error sending OTP. Check email configuration.', 'error')
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

handler = Mangum(app)

if __name__ == '__main__':
    app.run(debug=True)