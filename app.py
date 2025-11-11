from flask import Flask, render_template, render_template_string, request, redirect, url_for, flash, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import uuid
import mimetypes
import io
from docx import Document
import base64

# -------------------------------
# Flask setup
# -------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key_here'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///files.db'
app.jinja_env.globals['datetime'] = datetime

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

# -------------------------------
# Database models
# -------------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True)
    password = db.Column(db.String(150))
    files = db.relationship('File', backref='owner', lazy=True)


class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(300), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    current_version_number = db.Column(db.Integer, default=1)
    shared_with = db.Column(db.Text, default='')
    shared_version_id = db.Column(db.Integer, db.ForeignKey('file_version.id'), nullable=True)
    shared_until = db.Column(db.DateTime, nullable=True)
    share_token = db.Column(db.String(36), unique=True, nullable=True)
    share_password = db.Column(db.String(150), nullable=True)
    deleted_for = db.Column(db.Text, default='')  # comma-separated usernames

    versions = db.relationship(
        'FileVersion',
        backref='file',
        lazy='dynamic',
        order_by='FileVersion.version_number',
        foreign_keys='FileVersion.file_id'
    )

    @property
    def current_version(self):
        return self.versions.order_by(FileVersion.version_number.desc()).first()




class FileVersion(db.Model):
    __tablename__ = 'file_version'

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey('file.id'), nullable=False)
    version_number = db.Column(db.Integer, nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    data = db.Column(db.LargeBinary, nullable=False)
    uploaded_by = db.Column(db.String(150), nullable=False)

# -------------------------------
# Login manager
# -------------------------------
@login_manager.user_loader
def load_user(user_id):
   return db.session.get(User, int(user_id))

# -------------------------------
# Helper
# -------------------------------
def can_access_file(file_obj):
    is_owner = file_obj.user_id == current_user.id
    is_shared = current_user.is_authenticated and current_user.username in file_obj.shared_with.split(',') \
        and (file_obj.shared_until is None or file_obj.shared_until > datetime.utcnow())
    return is_owner or is_shared

# -------------------------------
# Routes
# -------------------------------
@app.route('/')
@login_required
def index():
    own_files = File.query.filter(
        File.user_id == current_user.id,
        ~File.deleted_for.contains(current_user.username)
    ).all()

    shared_files = File.query.filter(
        File.shared_with.contains(current_user.username),
        ((File.shared_until.is_(None)) | (File.shared_until > datetime.utcnow())),
        ~File.deleted_for.contains(current_user.username)
    ).all()

    all_files = own_files + shared_files
    return render_template('index.html', files=all_files, shared_files=shared_files)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = generate_password_hash(request.form['password'])
        if User.query.filter_by(username=username).first():
            flash("Username already exists!")
            return redirect(url_for('register'))
        new_user = User(username=username, password=password)
        db.session.add(new_user)
        db.session.commit()
        flash("Registered successfully! Please login.")
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if not user or not check_password_hash(user.password, password):
            flash("Invalid credentials")
            return redirect(url_for('login'))
        login_user(user)
        return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/search', methods=['POST'])
def search():
    query = request.form.get('query', '')

    if not query:
        return render_template('search_results.html', results=[], query=query)

    # Files owned by the current user
    owned_files = File.query.filter(
        File.user_id == current_user.id,
        File.filename.ilike(f"%{query}%")
    ).all()

    # Files shared with the current user
    # assuming you have a column like `shared_with` storing usernames (CSV or JSON)
    shared_files = File.query.filter(
        File.shared_with.ilike(f"%{current_user.username}%"),
        File.filename.ilike(f"%{query}%")
    ).all()

    # Combine results
    results = owned_files + shared_files

    return render_template('search_results.html', results=results, query=query)

# -------------------------------
# Upload
# -------------------------------
@app.route('/upload', methods=['POST'])
@login_required
def upload():
    uploaded_file = request.files.get('file')
    if not uploaded_file or uploaded_file.filename == '':
        flash("No file selected!")
        return redirect(url_for('index'))

    filename = secure_filename(uploaded_file.filename)
    existing_file = File.query.filter_by(filename=filename, user_id=current_user.id).first()
    file_data = uploaded_file.read()

    if existing_file:
        # 🧩 FIX: If the user had previously deleted the file for themselves, restore it
        deleted_users = existing_file.deleted_for.split(',') if existing_file.deleted_for else []
        if current_user.username in deleted_users:
            deleted_users.remove(current_user.username)
            existing_file.deleted_for = ','.join([u for u in deleted_users if u.strip()])

        # Increment version number
        version_number = (existing_file.current_version_number or 0) + 1
    else:
        # Create a new file record
        existing_file = File(filename=filename, user_id=current_user.id, current_version_number=1)
        db.session.add(existing_file)
        db.session.commit()
        version_number = 1

    # Create new version
    new_version = FileVersion(
        file_id=existing_file.id,
        version_number=version_number,
        data=file_data,
        uploaded_by=current_user.username
    )

    db.session.add(new_version)
    existing_file.current_version_number = version_number
    db.session.commit()

    flash(f"Uploaded '{filename}' as version v{version_number}.")
    return redirect(url_for('index'))

# -------------------------------
# History
# -------------------------------
@app.route('/history/<int:file_id>')
@login_required
def history(file_id):
    # Get the file or 404
    file_obj = File.query.get_or_404(file_id)

    # Determine if the current user is the owner
    is_owner = file_obj.user_id == current_user.id

    # Get all versions of this file, ordered by version number descending
    versions = FileVersion.query.filter_by(file_id=file_id).order_by(FileVersion.version_number.desc()).all()

    # Render the history template
    return render_template(
        'history.html',
        file=file_obj,
        versions=versions,
        is_owner=is_owner
    )

@app.route('/advantages')
def advantages():
    return render_template('advantages.html')



# -------------------------------
# Download
# -------------------------------
@app.route('/download_version/<int:version_id>')
@login_required
def download_version(version_id):
    version = FileVersion.query.get_or_404(version_id)
    file_obj = File.query.get_or_404(version.file_id)

    if file_obj.user_id == current_user.id:
        # Owner can download the requested version
        pass
    elif current_user.username in file_obj.shared_with.split(','):
        # Shared users can only download latest version
        version = file_obj.current_version
    else:
        flash("Access denied!")
        return redirect(url_for('index'))

    return Response(version.data,
                    headers={"Content-Disposition": f"attachment; filename={file_obj.filename}",
                             "Content-Type": "application/octet-stream"})



    
@app.route('/rollback/<int:file_id>/<int:version_id>', methods=['POST'])
@login_required
def rollback_version(file_id, version_id):
    file_obj = File.query.get_or_404(file_id)
    target_version = FileVersion.query.get_or_404(version_id)

    # Only owner can rollback
    if file_obj.user_id != current_user.id:
        flash("Unauthorized action.", "danger")
        return redirect(url_for('history', file_id=file_id))

    # Delete all versions newer than the target version
    newer_versions = FileVersion.query.filter(
        FileVersion.file_id == file_obj.id,
        FileVersion.version_number > target_version.version_number
    ).all()

    for v in newer_versions:
        db.session.delete(v)

    # Update the file's current version number
    file_obj.current_version_number = target_version.version_number

    db.session.commit()

    flash(f"File successfully rolled back to version v{target_version.version_number}.", "success")
    return redirect(url_for('history', file_id=file_id))




# -------------------------------
# Universal preview
# -------------------------------
@app.route('/preview/<int:version_id>')
@login_required
def preview(version_id):
    version = FileVersion.query.get_or_404(version_id)
    file_obj = File.query.get_or_404(version.file_id)

    if not can_access_file(file_obj):
        flash("Access denied!")
        return redirect(url_for('index'))

    mime_type, _ = mimetypes.guess_type(file_obj.filename)
    mime_type = mime_type or "application/octet-stream"

    text_content = None
    b64_data = None

    if mime_type.startswith('text'):
        try:
            text_content = version.data.decode('utf-8', errors='ignore')
        except:
            text_content = "Unable to decode text content."
    elif file_obj.filename.lower().endswith('.docx'):
        try:
            doc = Document(io.BytesIO(version.data))
            text_content = "\n".join([para.text for para in doc.paragraphs])
        except:
            text_content = "Unable to read DOCX file."
    elif mime_type.startswith('image') or mime_type == 'application/pdf':
        b64_data = base64.b64encode(version.data).decode('utf-8')

    return render_template('preview.html', file=file_obj, version=version,
                           mime_type=mime_type, text_content=text_content,
                           b64_data=b64_data)


# -------------------------------
# Delete file with owner choice
# -------------------------------
@app.route('/delete_version/<int:version_id>', methods=['POST'])
@login_required
def delete_version(version_id):
    # Get the version
    version = FileVersion.query.get_or_404(version_id)
    
    # Get the associated file
    file_obj = File.query.get_or_404(version.file_id)
    
    # Only allow the owner to delete
    if file_obj.user_id != current_user.id:
        flash("You are not authorized to delete this version.", "danger")
        return redirect(url_for('history', file_id=file_obj.id))

    # Delete the version
    db.session.delete(version)
    db.session.commit()
    flash(f"Version {version.version_number} deleted successfully.", "success")
    
    return redirect(url_for('history', file_id=file_obj.id))

@app.route('/delete_file/<int:file_id>', methods=['POST'])
@login_required
def delete_file(file_id):
    file_obj = File.query.get_or_404(file_id)
    delete_option = request.form.get('delete_option', 'self')  # default to 'self'

    if delete_option == 'self':
        # Mark the file as deleted for current user
        deleted_users = file_obj.deleted_for.split(',') if file_obj.deleted_for else []
        if current_user.username not in deleted_users:
            deleted_users.append(current_user.username)
            file_obj.deleted_for = ','.join(deleted_users)
            db.session.commit()
        flash(f"File '{file_obj.filename}' deleted for you only.", "success")
    elif delete_option == 'full':
        # Only owner can delete for everyone
        if file_obj.user_id != current_user.id:
            flash("You are not authorized to delete this file for everyone.", "danger")
            return redirect(url_for('index'))

        # Delete all versions first
        FileVersion.query.filter_by(file_id=file_id).delete()
        # Delete the file itself
        db.session.delete(file_obj)
        db.session.commit()
        flash(f"File '{file_obj.filename}' deleted for everyone.", "success")
    else:
        flash("Invalid delete option.", "danger")

    return redirect(url_for('index'))



# -------------------------------
# Share file
# -------------------------------
@app.route('/share/<int:file_id>', methods=['GET', 'POST'])
@login_required
def share(file_id):
    file_obj = File.query.get_or_404(file_id)
    if file_obj.user_id != current_user.id:
        flash("Access denied!")
        return redirect(url_for('index'))

    if request.method == 'POST':
        usernames = request.form.get('usernames', '')
        usernames_list = [u.strip() for u in usernames.split(',') if u.strip()]

        # ✅ Validate existing users
        existing_users = [u.username for u in User.query.filter(User.username.in_(usernames_list)).all()]
        invalid_users = set(usernames_list) - set(existing_users)

        if invalid_users:
            flash(f"The following users do not exist: {', '.join(invalid_users)}", "danger")
            return redirect(url_for('share', file_id=file_id))

        # ✅ Only store valid users
        file_obj.shared_with = ','.join(existing_users)

        days = int(request.form.get('expiry_days', 0))
        file_obj.shared_until = datetime.utcnow() + timedelta(days=days) if days > 0 else None

        make_public = request.form.get('make_public')
        password = request.form.get('public_password')

        if make_public:
            if not file_obj.share_token:
                file_obj.share_token = str(uuid.uuid4())
            file_obj.share_password = generate_password_hash(password) if password else None
            public_link = url_for('public_access', share_token=file_obj.share_token, _external=True)
            flash(
                f"File '{file_obj.filename}' is now public!<br>"
                f"<strong>Public Link:</strong> <a href='{public_link}' target='_blank'>{public_link}</a>",
                "success"
            )
        else:
            file_obj.share_token = None
            file_obj.share_password = None
            flash(f"File '{file_obj.filename}' share settings updated (Public access disabled).", "info")

        db.session.commit()
        return redirect(url_for('index'))

    return render_template('share.html', file=file_obj)


# -------------------------------
# Public access
# -------------------------------
@app.route('/public/<share_token>', methods=['GET', 'POST'])
def public_access(share_token):
    file_obj = File.query.filter_by(share_token=share_token).first_or_404()
    if file_obj.shared_until and datetime.utcnow() > file_obj.shared_until:
        flash("This shared link has expired.")
        return redirect(url_for('login'))

    if file_obj.share_password:
        if request.method == 'POST':
            password = request.form.get('password')
            if not check_password_hash(file_obj.share_password, password):
                flash("Incorrect password.")
                return redirect(request.url)
        else:
            return render_template('public_password.html', file_obj=file_obj)

    latest_version = FileVersion.query.filter_by(file_id=file_obj.id).order_by(FileVersion.version_number.desc()).first()
    if not latest_version:
        flash("No file available.")
        return redirect(url_for('login'))

    return Response(latest_version.data,
                    headers={"Content-Disposition": f"attachment; filename={file_obj.filename}",
                             "Content-Type": "application/octet-stream"})



# -------------------------------
# Initialize DB
# -------------------------------
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
